import asyncio
import websockets
import websockets.exceptions  # submodules are lazy in websockets>=14; import explicitly
import json
import logging
import platform
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List
import signal
import yaml

# Import collectors
from collectors.system_collector import SystemCollector
from collectors.network_collector import NetworkCollector
from collectors.process_collector import ProcessCollector
from collectors.filesystem_collector import FilesystemCollector
from platform_utils import PlatformUtils

REMOTE_CONFIG_KEYS = {'collection_interval', 'critical_services', 'connectivity_test_hosts'}

class SystemMonitorAgent:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.agent_id = config.get('agent_id', f"agent_{platform.node()}")
        self.server_url = config.get('server_url', 'ws://localhost:8000')
        self.collection_interval = config.get('collection_interval', 30)
        self.websocket = None
        # (agent_id, boot_id, seq) identifies every message; the server dedups on it.
        self.boot_id = uuid.uuid4().hex
        self.seq = 0
        self.throttled_until = 0.0
        self.running = False
        
        # Initialize collectors
        self.system_collector = SystemCollector()
        self.network_collector = NetworkCollector()
        self.process_collector = ProcessCollector()
        self.filesystem_collector = FilesystemCollector()
        
        # Platform utilities for cross-platform operations
        self.platform_utils = PlatformUtils()
        
        # Setup logging
        self.setup_logging()
        
        # Remediation defaults to dry-run: report what would be done, touch nothing.
        self.remediation_dry_run = config.get('remediation_dry_run', True)

    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(f'{self.agent_id}.log'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(f'SystemAgent-{self.agent_id}')

    async def start(self):
        """Start the monitoring agent"""
        self.running = True
        self.logger.info(f"Starting System Monitor Agent {self.agent_id}")
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        while self.running:
            try:
                await self.connect_and_monitor()
            except Exception as e:
                self.logger.error(f"Agent error: {e}")
                self.logger.info("Retrying connection in 30 seconds...")
                await asyncio.sleep(30)

    async def connect_and_monitor(self):
        """Connect to server and start monitoring"""
        uri = f"{self.server_url}/ws/agent/{self.agent_id}"
        
        try:
            async with websockets.connect(uri) as websocket:
                self.websocket = websocket
                self.logger.info(f"Connected to server: {self.server_url}")
                
                # Start monitoring tasks
                tasks = [
                    asyncio.create_task(self.collect_and_send_metrics()),
                    asyncio.create_task(self.handle_server_commands())
                ]
                
                await asyncio.gather(*tasks)
                
        except websockets.exceptions.ConnectionClosed:
            self.logger.warning("Connection to server lost")
        except Exception as e:
            self.logger.error(f"Connection error: {e}")
            raise

    async def collect_and_send_metrics(self):
        """Collect system metrics and send to server"""
        while self.running and self.websocket:
            try:
                # Collect comprehensive system metrics
                metrics = await self.collect_all_metrics()
                
                # Send metrics to server
                await self.websocket.send(json.dumps(metrics))
                
                await asyncio.sleep(max(self.collection_interval, self.throttled_until - time.monotonic()))
                
            except Exception as e:
                self.logger.error(f"Error collecting/sending metrics: {e}")
                break

    async def collect_all_metrics(self) -> Dict[str, Any]:
        """Collect all system metrics"""
        self.seq += 1
        metrics = {
            'agent_id': self.agent_id,
            'boot_id': self.boot_id,
            'seq': self.seq,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'platform': {
                'system': platform.system(),
                'node': platform.node(),
                'release': platform.release(),
                'machine': platform.machine()
            }
        }
        
        try:
            # System metrics (CPU, Memory, etc.)
            cpu_data = await self.system_collector.get_cpu_metrics()
            memory_data = await self.system_collector.get_memory_metrics()
            disk_data = await self.filesystem_collector.get_disk_metrics()
            network_data = await self.network_collector.get_network_metrics()
            
            # ✅ EXTRACT THE CORRECT KEYS
            metrics['cpu_usage'] = cpu_data.get('usage_percent', 0)
            metrics['memory_usage'] = memory_data.get('usage_percent', 0)
            metrics['disk_usage'] = disk_data.get('usage_percent', 0)
            metrics['network_latency'] = network_data.get('latency_ms', 0)
            
            # Keep full data too
            metrics['cpu'] = cpu_data
            metrics['memory'] = memory_data
            metrics['disk'] = disk_data
            metrics['network'] = network_data
            
            # Process metrics
            metrics['processes'] = await self.process_collector.get_top_processes()
            
            # System health indicators
            metrics['health'] = await self.get_health_indicators()
            
        except Exception as e:
            self.logger.error(f"Error collecting metrics: {e}")
            metrics['error'] = str(e)
            # Set defaults on error
            metrics['cpu_usage'] = 0
            metrics['memory_usage'] = 0
            metrics['disk_usage'] = 0
            metrics['network_latency'] = 0
        
        return metrics

    async def get_health_indicators(self) -> Dict[str, Any]:
        """Get additional health indicators"""
        try:
            health = {
                'uptime': await self.platform_utils.get_uptime(),
                'load_average': await self.platform_utils.get_load_average(),
                'service_status': await self.check_critical_services(),
                'disk_health': await self.check_disk_health(),
                'network_connectivity': await self.check_network_connectivity()
            }
            return health
        except Exception as e:
            self.logger.error(f"Error getting health indicators: {e}")
            return {}

    async def check_critical_services(self) -> List[Dict[str, str]]:
        """Check status of critical system services"""
        critical_services = self.config.get('critical_services', [])
        service_status = []
        
        for service in critical_services:
            try:
                status = await self.platform_utils.get_service_status(service)
                service_status.append({
                    'name': service,
                    'status': status,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
            except Exception as e:
                service_status.append({
                    'name': service,
                    'status': 'error',
                    'error': str(e),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
        
        return service_status

    async def check_disk_health(self) -> Dict[str, Any]:
        """Check disk health indicators"""
        try:
            return {
                'io_stats': await self.filesystem_collector.get_io_stats(),
                'mount_points': await self.filesystem_collector.get_mount_points(),
                'disk_errors': await self.platform_utils.get_disk_errors()
            }
        except Exception as e:
            self.logger.error(f"Error checking disk health: {e}")
            return {}

    async def check_network_connectivity(self) -> Dict[str, Any]:
        """Check network connectivity"""
        try:
            connectivity = {}
            test_hosts = self.config.get('connectivity_test_hosts', ['8.8.8.8', 'google.com'])
            
            for host in test_hosts:
                result = await self.network_collector.ping_host(host)
                connectivity[host] = result
            
            return connectivity
        except Exception as e:
            self.logger.error(f"Error checking network connectivity: {e}")
            return {}

    async def handle_server_commands(self):
        """Handle commands from server"""
        while self.running and self.websocket:
            try:
                message = await self.websocket.recv()
                command = json.loads(message)
                
                await self.process_command(command)
                
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as e:
                self.logger.error(f"Error handling server command: {e}")

    async def process_command(self, command: Dict[str, Any]):
        """Process command from server"""
        try:
            command_type = command.get('type')
            
            if command_type == 'restart':
                await self.restart_agent()
            elif command_type == 'remediate':
                await self.execute_remediation(command.get('issue_type'))
            elif command_type == 'update_config':
                await self.update_config(command.get('config'))
            elif command_type == 'throttle':
                # Server backlog is above its limit: back off instead of piling on.
                retry = float(command.get('retry_after_s', 5))
                self.throttled_until = time.monotonic() + retry
                self.logger.warning(f"Server asked to back off for {retry}s: {command.get('reason')}")
            elif command_type == 'error':
                self.logger.warning(f"Server rejected a message: {command.get('reason')}")
            elif command_type == 'ping':
                await self.websocket.send(json.dumps({'type': 'pong'}))
            else:
                self.logger.warning(f"Unknown command type: {command_type}")
                
        except Exception as e:
            self.logger.error(f"Error processing command: {e}")

    async def execute_remediation(self, issue_type: str):
        """Execute auto-remediation for specific issue"""
        try:
            self.logger.info(f"Executing remediation for: {issue_type}")
            
            # A fixed allowlist of actions; the server can only pick one, never send code.
            remediation_map = {
                'cpu_threshold_breach': self._remediate_high_cpu,
                'disk_threshold_breach': self._remediate_disk,
            }

            action = remediation_map.get(issue_type)
            if action is None:
                self.logger.warning(f"No remediation action for issue: {issue_type}")
                return

            result = await action()
            await self.websocket.send(json.dumps({
                'type': 'remediation_result',
                'issue_type': issue_type,
                'dry_run': self.remediation_dry_run,
                'success': result.get('success', False),
                'output': result,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }))

        except Exception as e:
            self.logger.error(f"Error executing remediation: {e}")

    async def _remediate_high_cpu(self) -> Dict[str, Any]:
        top = await self.process_collector.get_top_processes(limit=5)
        if self.remediation_dry_run:
            return {'success': True, 'would_inspect': top}
        killed = await self.process_collector.kill_high_cpu_processes(
            cpu_threshold=self.config.get('kill_cpu_threshold', 90.0)
        )
        return {'success': True, 'killed': killed}

    async def _remediate_disk(self) -> Dict[str, Any]:
        if self.remediation_dry_run:
            return {'success': True, 'would_clean': 'temp files older than 1 day'}
        return await self.filesystem_collector.cleanup_temp_files()

    async def update_config(self, new_config: Dict[str, Any]):
        """Update agent configuration.

        Only tuning keys may be changed remotely; safety settings such as
        remediation_dry_run are local-only so the server cannot switch them off.
        """
        try:
            accepted = {k: v for k, v in (new_config or {}).items() if k in REMOTE_CONFIG_KEYS}
            rejected = sorted(set(new_config or {}) - set(accepted))
            if rejected:
                self.logger.warning(f"Ignoring non-remote-configurable keys: {rejected}")

            self.config.update(accepted)
            self.collection_interval = self.config.get('collection_interval', 30)
            self.logger.info("Configuration updated successfully")

            # Send confirmation
            await self.websocket.send(json.dumps({
                'type': 'config_updated',
                'applied': sorted(accepted),
                'rejected': rejected,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }))
            
        except Exception as e:
            self.logger.error(f"Error updating configuration: {e}")

    async def restart_agent(self):
        """Restart the agent"""
        try:
            self.logger.info("Restarting agent...")
            self.running = False
            
            # Close websocket connection
            if self.websocket:
                await self.websocket.close()
            
            # In production, implement proper restart mechanism
            os.execv(sys.executable, ['python'] + sys.argv)
            
        except Exception as e:
            self.logger.error(f"Error restarting agent: {e}")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    async def shutdown(self):
        """Graceful shutdown"""
        self.logger.info("Shutting down agent...")
        self.running = False
        
        if self.websocket:
            await self.websocket.close()
    
def load_config(path: str = 'config.yaml') -> Dict[str, Any]:
    """config.yaml is optional; AGENT_ID / SERVER_URL env vars override it."""
    config: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8-sig') as f:  # tolerate BOM from Windows editors
            config = yaml.safe_load(f) or {}
    for key, env in (('agent_id', 'AGENT_ID'), ('server_url', 'SERVER_URL')):
        if os.getenv(env):
            config[key] = os.environ[env]
    return config


if __name__ == "__main__":
    agent = SystemMonitorAgent(load_config())
    asyncio.run(agent.start())