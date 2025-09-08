
from typing import Dict, List, Any
from datetime import datetime, timedelta
import asyncio
import logging

logger = logging.getLogger(__name__)

class AlertManager:
    def __init__(self):
        self.active_alerts = {}
        self.alert_history = []
        self.notification_channels = ['webhook', 'email', 'slack']
        self.alert_rules = {
            'cpu_high': {'threshold': 80, 'duration': 300, 'severity': 'warning'},
            'cpu_critical': {'threshold': 95, 'duration': 60, 'severity': 'critical'},
            'memory_high': {'threshold': 85, 'duration': 300, 'severity': 'warning'},
            'memory_critical': {'threshold': 95, 'duration': 60, 'severity': 'critical'},
            'disk_critical': {'threshold': 90, 'duration': 0, 'severity': 'critical'}
        }

    async def process_alert(self, alert_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process and manage alerts"""
        try:
            alert_id = f"{alert_data['type']}_{alert_data.get('agent_id', 'unknown')}"
            
            if alert_id in self.active_alerts:
                # Update existing alert
                self.active_alerts[alert_id]['last_seen'] = datetime.now()
                self.active_alerts[alert_id]['count'] += 1
            else:
                # Create new alert
                self.active_alerts[alert_id] = {
                    'id': alert_id,
                    'type': alert_data['type'],
                    'severity': alert_data.get('severity', 'medium'),
                    'description': alert_data.get('description', ''),
                    'first_seen': datetime.now(),
                    'last_seen': datetime.now(),
                    'count': 1,
                    'status': 'active',
                    'agent_id': alert_data.get('agent_id'),
                    'suggested_action': alert_data.get('suggested_action')
                }
                
                # Add to history
                self.alert_history.append(self.active_alerts[alert_id].copy())
            
            # Send notifications for critical alerts
            if alert_data.get('severity') == 'critical':
                await self._send_notifications(self.active_alerts[alert_id])
            
            return self.active_alerts[alert_id]
            
        except Exception as e:
            logger.error(f"Error processing alert: {e}")
            return {}

    async def _send_notifications(self, alert: Dict[str, Any]):
        """Send alert notifications"""
        try:
            # In production, integrate with actual notification services
            logger.info(f"CRITICAL ALERT: {alert['description']}")
            
            # Webhook notification
            # await self._send_webhook(alert)
            
            # Email notification  
            # await self._send_email(alert)
            
            # Slack notification
            # await self._send_slack(alert)
            
        except Exception as e:
            logger.error(f"Error sending notifications: {e}")

    async def resolve_alert(self, alert_id: str) -> bool:
        """Resolve an active alert"""
        try:
            if alert_id in self.active_alerts:
                self.active_alerts[alert_id]['status'] = 'resolved'
                self.active_alerts[alert_id]['resolved_at'] = datetime.now()
                del self.active_alerts[alert_id]
                return True
            return False
        except Exception as e:
            logger.error(f"Error resolving alert: {e}")
            return False

    async def get_active_alerts(self) -> List[Dict[str, Any]]:
        """Get all active alerts"""
        return list(self.active_alerts.values())

    async def get_alert_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get alert history"""
        return self.alert_history[-limit:]

    async def cleanup_old_alerts(self):
        """Clean up resolved alerts older than 24 hours"""
        try:
            cutoff_time = datetime.now() - timedelta(hours=24)
            alerts_to_remove = []
            
            for alert_id, alert in self.active_alerts.items():
                if alert.get('status') == 'resolved' and alert.get('resolved_at', datetime.now()) < cutoff_time:
                    alerts_to_remove.append(alert_id)
            
            for alert_id in alerts_to_remove:
                del self.active_alerts[alert_id]
                
        except Exception as e:
            logger.error(f"Error cleaning up alerts: {e}")