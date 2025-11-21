# backend/app/services/ai_engine.py
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import DBSCAN
import joblib
import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import logging
from collections import deque
import json

logger = logging.getLogger(__name__)

class AIEngine:
    def __init__(self):
        self.anomaly_models = {}
        self.scalers = {}
        self.historical_data = deque(maxlen=1000)  # Store last 1000 data points
        self.thresholds = {
            'cpu_usage': 80.0,
            'memory_usage': 85.0,
            'disk_usage': 90.0,
            'network_latency': 200.0  # milliseconds
        }
        self.patterns = {}
        self.initialized = False

    async def initialize(self):
        """Initialize AI models and load pre-trained weights"""
        try:
            # Initialize anomaly detection models
            self.anomaly_models = {
                'system_metrics': IsolationForest(
                    contamination=0.1,
                    random_state=42,
                    n_estimators=100
                ),
                'network_metrics': IsolationForest(
                    contamination=0.05,
                    random_state=42,
                    n_estimators=50
                )
            }
            
            # Initialize scalers
            self.scalers = {
                'system_metrics': StandardScaler(),
                'network_metrics': StandardScaler()
            }
            
            # Load pre-existing models if available
            await self._load_models()
            
            self.initialized = True
            logger.info("AI Engine initialized successfully")
            
        except Exception as e:
            logger.error(f"Error initializing AI Engine: {e}")
            raise

    async def _load_models(self):
        """Load pre-trained models from disk"""
        try:
            # In production, load from persistent storage
            # For demo, we'll use default initialized models
            pass
        except Exception as e:
            logger.warning(f"Could not load pre-trained models: {e}")

    async def detect_anomalies(self, metrics_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Detect anomalies in real-time metrics data"""
        if not self.initialized:
            return []
        
        try:
            anomalies = []
            
            # Store historical data
            self.historical_data.append({
                'timestamp': datetime.now(),
                'data': metrics_data
            })
            
            # Extract system metrics
            system_features = self._extract_system_features(metrics_data)
            if system_features:
                system_anomalies = await self._detect_system_anomalies(system_features)
                anomalies.extend(system_anomalies)
            
            # Extract network metrics
            network_features = self._extract_network_features(metrics_data)
            if network_features:
                network_anomalies = await self._detect_network_anomalies(network_features)
                anomalies.extend(network_anomalies)
            
            # Detect threshold breaches
            threshold_anomalies = self._detect_threshold_anomalies(metrics_data)
            anomalies.extend(threshold_anomalies)
            
            # Detect pattern anomalies
            pattern_anomalies = await self._detect_pattern_anomalies(metrics_data)
            anomalies.extend(pattern_anomalies)
            
            return anomalies
            
        except Exception as e:
            logger.error(f"Error detecting anomalies: {e}")
            return []

    def _extract_system_features(self, metrics_data: Dict[str, Any]) -> Optional[np.ndarray]:
        """Extract numerical features for system metrics"""
        try:
            features = []
            
            # CPU metrics
            if 'cpu' in metrics_data:
                cpu_data = metrics_data['cpu']
                features.extend([
                    cpu_data.get('usage_percent', 0),
                    cpu_data.get('load_avg_1m', 0),
                    cpu_data.get('load_avg_5m', 0),
                    cpu_data.get('context_switches', 0)
                ])
            
            # Memory metrics
            if 'memory' in metrics_data:
                mem_data = metrics_data['memory']
                features.extend([
                    mem_data.get('usage_percent', 0),
                    mem_data.get('available_mb', 0),
                    mem_data.get('swap_usage_percent', 0)
                ])
            
            # Disk metrics
            if 'disk' in metrics_data:
                disk_data = metrics_data['disk']
                features.extend([
                    disk_data.get('usage_percent', 0),
                    disk_data.get('read_bytes_per_sec', 0),
                    disk_data.get('write_bytes_per_sec', 0),
                    disk_data.get('io_wait_percent', 0)
                ])
            
            return np.array(features).reshape(1, -1) if features else None
            
        except Exception as e:
            logger.error(f"Error extracting system features: {e}")
            return None

    def _extract_network_features(self, metrics_data: Dict[str, Any]) -> Optional[np.ndarray]:
        """Extract numerical features for network metrics"""
        try:
            features = []
            
            if 'network' in metrics_data:
                net_data = metrics_data['network']
                features.extend([
                    net_data.get('latency_ms', 0),
                    net_data.get('packet_loss_percent', 0),
                    net_data.get('bandwidth_usage_percent', 0),
                    net_data.get('connections_count', 0),
                    net_data.get('bytes_sent_per_sec', 0),
                    net_data.get('bytes_recv_per_sec', 0)
                ])
            
            return np.array(features).reshape(1, -1) if features else None
            
        except Exception as e:
            logger.error(f"Error extracting network features: {e}")
            return None

    async def _detect_system_anomalies(self, features: np.ndarray) -> List[Dict[str, Any]]:
        """Detect system-level anomalies using ML models"""
        try:
            anomalies = []
            
            # Check if we have enough historical data to retrain
            if len(self.historical_data) > 50:
                # Prepare training data
                training_features = []
                for entry in list(self.historical_data)[-50:]:
                    hist_features = self._extract_system_features(entry['data'])
                    if hist_features is not None:
                        training_features.append(hist_features.flatten())
                
                if len(training_features) > 10:
                    training_data = np.array(training_features)
                    
                    # Scale and train
                    scaled_data = self.scalers['system_metrics'].fit_transform(training_data)
                    self.anomaly_models['system_metrics'].fit(scaled_data)
                    
                    # Predict on current features
                    scaled_features = self.scalers['system_metrics'].transform(features)
                    anomaly_score = self.anomaly_models['system_metrics'].decision_function(scaled_features)
                    is_anomaly = self.anomaly_models['system_metrics'].predict(scaled_features)
                    
                    if is_anomaly[0] == -1:  # Anomaly detected
                        anomalies.append({
                            'type': 'system_anomaly',
                            'severity': 'high' if anomaly_score[0] < -0.5 else 'medium',
                            'score': float(anomaly_score[0]),
                            'description': 'Unusual system behavior detected by ML model',
                            'suggested_action': 'investigate_system_resources'
                        })
            
            return anomalies
            
        except Exception as e:
            logger.error(f"Error in system anomaly detection: {e}")
            return []

    async def _detect_network_anomalies(self, features: np.ndarray) -> List[Dict[str, Any]]:
        """Detect network-level anomalies"""
        try:
            anomalies = []
            
            # Similar to system anomalies but for network metrics
            if len(self.historical_data) > 30:
                training_features = []
                for entry in list(self.historical_data)[-30:]:
                    hist_features = self._extract_network_features(entry['data'])
                    if hist_features is not None:
                        training_features.append(hist_features.flatten())
                
                if len(training_features) > 10:
                    training_data = np.array(training_features)
                    scaled_data = self.scalers['network_metrics'].fit_transform(training_data)
                    self.anomaly_models['network_metrics'].fit(scaled_data)
                    
                    scaled_features = self.scalers['network_metrics'].transform(features)
                    anomaly_score = self.anomaly_models['network_metrics'].decision_function(scaled_features)
                    is_anomaly = self.anomaly_models['network_metrics'].predict(scaled_features)
                    
                    if is_anomaly[0] == -1:
                        anomalies.append({
                            'type': 'network_anomaly',
                            'severity': 'high' if anomaly_score[0] < -0.3 else 'medium',
                            'score': float(anomaly_score[0]),
                            'description': 'Unusual network behavior detected',
                            'suggested_action': 'check_network_connectivity'
                        })
            
            return anomalies
            
        except Exception as e:
            logger.error(f"Error in network anomaly detection: {e}")
            return []

    def _detect_threshold_anomalies(self, metrics_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Detect simple threshold-based anomalies"""
        anomalies = []
        
        try:
            # CPU threshold check
            if 'cpu' in metrics_data:
                cpu_usage = metrics_data['cpu'].get('usage_percent', 0)
                if cpu_usage > self.thresholds['cpu_usage']:
                    anomalies.append({
                        'type': 'cpu_threshold_breach',
                        'severity': 'critical' if cpu_usage > 95 else 'high',
                        'value': cpu_usage,
                        'threshold': self.thresholds['cpu_usage'],
                        'description': f'CPU usage {cpu_usage}% exceeds threshold',
                        'suggested_action': 'identify_cpu_intensive_processes'
                    })
            
            # Memory threshold check
            if 'memory' in metrics_data:
                mem_usage = metrics_data['memory'].get('usage_percent', 0)
                if mem_usage > self.thresholds['memory_usage']:
                    anomalies.append({
                        'type': 'memory_threshold_breach',
                        'severity': 'critical' if mem_usage > 95 else 'high',
                        'value': mem_usage,
                        'threshold': self.thresholds['memory_usage'],
                        'description': f'Memory usage {mem_usage}% exceeds threshold',
                        'suggested_action': 'free_memory_resources'
                    })
            
            # Disk threshold check
            if 'disk' in metrics_data:
                disk_usage = metrics_data['disk'].get('usage_percent', 0)
                if disk_usage > self.thresholds['disk_usage']:
                    anomalies.append({
                        'type': 'disk_threshold_breach',
                        'severity': 'critical' if disk_usage > 98 else 'high',
                        'value': disk_usage,
                        'threshold': self.thresholds['disk_usage'],
                        'description': f'Disk usage {disk_usage}% exceeds threshold',
                        'suggested_action': 'cleanup_disk_space'
                    })
            
            # Network latency check
            if 'network' in metrics_data:
                latency = metrics_data['network'].get('latency_ms', 0)
                if latency > self.thresholds['network_latency']:
                    anomalies.append({
                        'type': 'network_latency_high',
                        'severity': 'medium',
                        'value': latency,
                        'threshold': self.thresholds['network_latency'],
                        'description': f'Network latency {latency}ms exceeds threshold',
                        'suggested_action': 'check_network_connectivity'
                    })
            
        except Exception as e:
            logger.error(f"Error in threshold anomaly detection: {e}")
        
        return anomalies

    async def _detect_pattern_anomalies(self, metrics_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Detect pattern-based anomalies (trends, cycles)"""
        try:
            anomalies = []
            
            # Only analyze patterns if we have sufficient historical data
            if len(self.historical_data) < 20:
                return anomalies
            
            # Analyze CPU usage trends
            cpu_values = []
            timestamps = []
            for entry in list(self.historical_data)[-20:]:
                if 'cpu' in entry['data']:
                    cpu_values.append(entry['data']['cpu'].get('usage_percent', 0))
                    timestamps.append(entry['timestamp'])
            
            if len(cpu_values) >= 15:
                # Detect rapid CPU increase (potential runaway process)
                recent_avg = np.mean(cpu_values[-5:])
                older_avg = np.mean(cpu_values[-15:-10])
                
                if recent_avg > older_avg + 30:  # 30% increase
                    anomalies.append({
                        'type': 'cpu_trend_anomaly',
                        'severity': 'high',
                        'description': f'Rapid CPU increase detected: {older_avg:.1f}% → {recent_avg:.1f}%',
                        'suggested_action': 'investigate_cpu_spike'
                    })
            
            # Analyze memory leak patterns
            memory_values = []
            for entry in list(self.historical_data)[-15:]:
                if 'memory' in entry['data']:
                    memory_values.append(entry['data']['memory'].get('usage_percent', 0))
            
            if len(memory_values) >= 10:
                # Check for consistent memory increase (potential memory leak)
                slope = np.polyfit(range(len(memory_values)), memory_values, 1)[0]
                if slope > 2:  # Memory increasing by >2% per measurement
                    anomalies.append({
                        'type': 'memory_leak_pattern',
                        'severity': 'high',
                        'description': f'Potential memory leak detected (trend: +{slope:.1f}% per interval)',
                        'suggested_action': 'investigate_memory_leak'
                    })
            
            return anomalies
            
        except Exception as e:
            logger.error(f"Error in pattern anomaly detection: {e}")
            return []

    async def predict_failure(self, metrics_data: Dict[str, Any]) -> Dict[str, Any]:
        """Predict potential system failures using ML"""
        try:
            predictions = {
                'disk_full_prediction': None,
                'memory_exhaustion_prediction': None,
                'cpu_overload_prediction': None
            }
            
            if len(self.historical_data) < 10:
                return predictions
            
            # Disk space prediction
            if 'disk' in metrics_data:
                disk_usage_history = []
                for entry in list(self.historical_data)[-10:]:
                    if 'disk' in entry['data']:
                        disk_usage_history.append(entry['data']['disk'].get('usage_percent', 0))
                
                if len(disk_usage_history) >= 5:
                    # Simple linear extrapolation
                    x = np.arange(len(disk_usage_history))
                    coeffs = np.polyfit(x, disk_usage_history, 1)
                    slope = coeffs[0]
                    current_usage = disk_usage_history[-1]
                    
                    if slope > 0.1:  # Disk usage increasing
                        time_to_full = (100 - current_usage) / slope
                        if time_to_full < 100:  # Less than 100 intervals
                            predictions['disk_full_prediction'] = {
                                'time_to_failure': int(time_to_full),
                                'confidence': min(0.9, slope * 10),
                                'current_usage': current_usage,
                                'trend': slope
                            }
            
            # Memory exhaustion prediction
            if 'memory' in metrics_data:
                memory_usage_history = []
                for entry in list(self.historical_data)[-10:]:
                    if 'memory' in entry['data']:
                        memory_usage_history.append(entry['data']['memory'].get('usage_percent', 0))
                
                if len(memory_usage_history) >= 5:
                    x = np.arange(len(memory_usage_history))
                    coeffs = np.polyfit(x, memory_usage_history, 1)
                    slope = coeffs[0]
                    current_usage = memory_usage_history[-1]
                    
                    if slope > 0.5:  # Memory usage increasing significantly
                        time_to_exhaustion = (95 - current_usage) / slope
                        if time_to_exhaustion < 50:
                            predictions['memory_exhaustion_prediction'] = {
                                'time_to_failure': int(time_to_exhaustion),
                                'confidence': min(0.85, slope * 5),
                                'current_usage': current_usage,
                                'trend': slope
                            }
            
            return predictions
            
        except Exception as e:
            logger.error(f"Error in failure prediction: {e}")
            return {}

    async def suggest_remediation(self, anomaly: Dict[str, Any]) -> Dict[str, Any]:
        """Suggest auto-remediation actions based on anomaly type"""
        remediation_actions = {
            'cpu_threshold_breach': {
                'actions': [
                    'kill_high_cpu_processes',
                    'restart_services',
                    'scale_horizontally'
                ],
                'scripts': ['kill_top_cpu_process.sh', 'restart_critical_services.sh'],
                'priority': 'high'
            },
            'memory_threshold_breach': {
                'actions': [
                    'clear_cache',
                    'restart_memory_intensive_services',
                    'enable_swap'
                ],
                'scripts': ['clear_system_cache.sh', 'restart_services.sh'],
                'priority': 'high'
            },
            'disk_threshold_breach': {
                'actions': [
                    'cleanup_temp_files',
                    'rotate_logs',
                    'compress_old_files'
                ],
                'scripts': ['disk_cleanup.sh', 'log_rotation.sh'],
                'priority': 'critical'
            },
            'network_latency_high': {
                'actions': [
                    'restart_network_services',
                    'flush_dns_cache',
                    'check_firewall_rules'
                ],
                'scripts': ['network_reset.sh', 'dns_flush.sh'],
                'priority': 'medium'
            },
            'memory_leak_pattern': {
                'actions': [
                    'restart_suspected_services',
                    'dump_memory_analysis',
                    'enable_memory_monitoring'
                ],
                'scripts': ['restart_leaky_services.sh', 'memory_dump.sh'],
                'priority': 'high'
            }
        }
        
        anomaly_type = anomaly.get('type', 'unknown')
        return remediation_actions.get(anomaly_type, {
            'actions': ['manual_investigation'],
            'scripts': [],
            'priority': 'low'
        })

    async def system_health_check(self) -> Dict[str, Any]:
        """Perform comprehensive system health assessment"""
        try:
            if not self.historical_data:
                return {"status": "insufficient_data", "critical_issues": []}
            
            latest_data = list(self.historical_data)[-1]['data']
            health_status = {
                "status": "healthy",
                "critical_issues": [],
                "warnings": [],
                "overall_score": 100,
                "component_scores": {}
            }
            
            # Check critical thresholds
            if 'cpu' in latest_data:
                cpu_usage = latest_data['cpu'].get('usage_percent', 0)
                if cpu_usage > 95:
                    health_status["critical_issues"].append({
                        "component": "cpu",
                        "issue": "Critical CPU usage",
                        "value": cpu_usage,
                        "impact": "System may become unresponsive"
                    })
                    health_status["overall_score"] -= 30
                elif cpu_usage > 80:
                    health_status["warnings"].append({
                        "component": "cpu",
                        "issue": "High CPU usage",
                        "value": cpu_usage
                    })
                    health_status["overall_score"] -= 10
                
                health_status["component_scores"]["cpu"] = max(0, 100 - cpu_usage)
            
            if 'memory' in latest_data:
                mem_usage = latest_data['memory'].get('usage_percent', 0)
                if mem_usage > 95:
                    health_status["critical_issues"].append({
                        "component": "memory",
                        "issue": "Critical memory usage",
                        "value": mem_usage,
                        "impact": "Risk of out-of-memory errors"
                    })
                    health_status["overall_score"] -= 25
                elif mem_usage > 85:
                    health_status["warnings"].append({
                        "component": "memory",
                        "issue": "High memory usage",
                        "value": mem_usage
                    })
                    health_status["overall_score"] -= 10
                
                health_status["component_scores"]["memory"] = max(0, 100 - mem_usage)
            
            if 'disk' in latest_data:
                disk_usage = latest_data['disk'].get('usage_percent', 0)
                if disk_usage > 98:
                    health_status["critical_issues"].append({
                        "component": "disk",
                        "issue": "Critical disk usage",
                        "value": disk_usage,
                        "impact": "System may fail to write data"
                    })
                    health_status["overall_score"] -= 35
                elif disk_usage > 90:
                    health_status["warnings"].append({
                        "component": "disk",
                        "issue": "High disk usage",
                        "value": disk_usage
                    })
                    health_status["overall_score"] -= 15
                
                health_status["component_scores"]["disk"] = max(0, 100 - disk_usage)
            
            # Set overall status
            if health_status["critical_issues"]:
                health_status["status"] = "critical"
            elif health_status["warnings"]:
                health_status["status"] = "warning"
            
            return health_status
            
        except Exception as e:
            logger.error(f"Error in system health check: {e}")
            return {
                "status": "error",
                "critical_issues": [{"component": "monitoring", "issue": "Health check failed"}],
                "overall_score": 0
            }

    async def get_performance_insights(self) -> Dict[str, Any]:
        """Generate performance insights and recommendations"""
        try:
            if len(self.historical_data) < 10:
                return {"insights": [], "recommendations": []}
            
            insights = {
                "insights": [],
                "recommendations": [],
                "trends": {},
                "efficiency_score": 85
            }
            
            # Analyze recent trends
            recent_data = list(self.historical_data)[-10:]
            
            # CPU trend analysis
            cpu_values = [entry['data'].get('cpu', {}).get('usage_percent', 0) for entry in recent_data]
            if cpu_values:
                cpu_trend = np.mean(cpu_values[-3:]) - np.mean(cpu_values[:3])
                insights["trends"]["cpu"] = {
                    "direction": "increasing" if cpu_trend > 5 else "decreasing" if cpu_trend < -5 else "stable",
                    "change": cpu_trend,
                    "average": np.mean(cpu_values)
                }
                
                if cpu_trend > 10:
                    insights["insights"].append("CPU usage has increased significantly in recent measurements")
                    insights["recommendations"].append("Consider investigating processes causing increased CPU usage")
            
            # Memory trend analysis
            mem_values = [entry['data'].get('memory', {}).get('usage_percent', 0) for entry in recent_data]
            if mem_values:
                mem_trend = np.mean(mem_values[-3:]) - np.mean(mem_values[:3])
                insights["trends"]["memory"] = {
                    "direction": "increasing" if mem_trend > 5 else "decreasing" if mem_trend < -5 else "stable",
                    "change": mem_trend,
                    "average": np.mean(mem_values)
                }
                
                if mem_trend > 8:
                    insights["insights"].append("Memory usage shows concerning upward trend")
                    insights["recommendations"].append("Monitor for potential memory leaks")
            
            # Performance score calculation
            if cpu_values and mem_values:
                avg_cpu = np.mean(cpu_values)
                avg_mem = np.mean(mem_values)
                efficiency_score = 100 - (avg_cpu * 0.4 + avg_mem * 0.4 + max(0, cpu_trend) * 0.2)
                insights["efficiency_score"] = max(0, int(efficiency_score))
            
            return insights
            
        except Exception as e:
            logger.error(f"Error generating performance insights: {e}")
            return {"insights": [], "recommendations": [], "trends": {}}

    async def save_models(self):
        """Save trained models to disk"""
        try:
            # In production, save to persistent storage
            joblib.dump(self.anomaly_models, 'models/anomaly_models.pkl')
            joblib.dump(self.scalers, 'models/scalers.pkl')
            logger.info("AI models saved successfully")
        except Exception as e:
            logger.error(f"Error saving models: {e}")
