import asyncio
import websockets
import json
import time
import logging
import platform
import os
import sys
from datetime import datetime
from typing import Dict, Any, List
import signal
import threading
import yaml

# Import collectors
from collectors.system_collector import SystemCollector
from collectors.network_collector import NetworkCollector
from collectors.process_collector import ProcessCollector
from collectors.filesystem_collector import FilesystemCollector
from platform_utils import PlatformUtils

class SystemMonitorAgent:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.agent_id = config.get('agent_id', f"agent_{platform.node()}")
        self.server_url = config.get('server_url', 'ws://localhost:8000')
        self.collection_interval = config.get('collection_interval', 30)
        self.websocket = None
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
        
        # Auto-remediation scripts
        self.remediation_scripts = self._load_remediation_scripts()
        
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

    def _load_remediation_scripts(self) -> Dict[str, str]:
        """Load platform-specific remediation scripts"""
        scripts = {}
        current_os = platform.system().lower()
        
        if current_os == 'linux':
            scripts = {
                'kill_high_cpu_process': 'scripts/linux/kill_high_cpu.sh',
                'cleanup_disk_space': 'scripts/linux/disk_cleanup.sh',
                'restart_services': 'scripts/linux/restart_services.sh',
                'clear_cache': 'scripts/linux/clear_cache.sh',
                'network_reset': 'scripts/linux/network_reset.sh'
            }
        elif current_os == 'windows':
            scripts = {
                'kill_high_cpu_process': 'scripts/windows/kill_high_cpu.bat',
                'cleanup_disk_space': 'scripts/windows/disk_cleanup.bat',
                'restart_services': 'scripts/windows/restart_services.bat',
                'clear_cache': 'scripts/windows/clear_cache.bat',
                'network_reset': 'scripts/windows/network_reset.bat'
            }
        
        return scripts

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
                
                await asyncio.sleep(self.collection_interval)
                
            except Exception as e:
                self.logger.error(f"Error collecting/sending metrics: {e}")
                break

    async def collect_all_metrics(self) -> Dict[str, Any]:
        """Collect all system metrics"""
        metrics = {
            'agent_id': self.agent_id,
            'timestamp': datetime.now().isoformat(),
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
                    'timestamp': datetime.now().isoformat()
                })
            except Exception as e:
                service_status.append({
                    'name': service,
                    'status': 'error',
                    'error': str(e),
                    'timestamp': datetime.now().isoformat()
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
            elif command_type == 'run_script':
                await self.run_custom_script(command.get('script'))
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
            
            remediation_map = {
                'cpu_threshold_breach': 'kill_high_cpu_process',
                'memory_threshold_breach': 'clear_cache',
                'disk_threshold_breach': 'cleanup_disk_space',
                'network_latency_high': 'network_reset'
            }
            
            script_key = remediation_map.get(issue_type)
            if script_key and script_key in self.remediation_scripts:
                script_path = self.remediation_scripts[script_key]
                result = await self.platform_utils.execute_script(script_path)
                
                # Send result back to server
                await self.websocket.send(json.dumps({
                    'type': 'remediation_result',
                    'issue_type': issue_type,
                    'success': result.get('success', False),
                    'output': result.get('output', ''),
                    'timestamp': datetime.now().isoformat()
                }))
            else:
                self.logger.warning(f"No remediation script for issue: {issue_type}")
                
        except Exception as e:
            self.logger.error(f"Error executing remediation: {e}")

    async def run_custom_script(self, script_content: str):
        """Run custom script sent from server"""
        try:
            if not self.config.get('allow_custom_scripts', False):
                self.logger.warning("Custom scripts not allowed by configuration")
                return
            
            # Security: Basic validation (in production, use more robust validation)
            if any(dangerous in script_content.lower() for dangerous in ['rm -rf', 'del /f', 'format']):
                self.logger.warning("Dangerous script detected, execution blocked")
                return
            
            # Execute script in controlled environment
            result = await self.platform_utils.execute_custom_script(script_content)
            
            # Send result back to server
            await self.websocket.send(json.dumps({
                'type': 'script_result',
                'success': result.get('success', False),
                'output': result.get('output', ''),
                'timestamp': datetime.now().isoformat()
            }))
            
        except Exception as e:
            self.logger.error(f"Error executing custom script: {e}")

    async def update_config(self, new_config: Dict[str, Any]):
        """Update agent configuration"""
        try:
            self.config.update(new_config)
            self.collection_interval = self.config.get('collection_interval', 30)
            self.logger.info("Configuration updated successfully")
            
            # Send confirmation
            await self.websocket.send(json.dumps({
                'type': 'config_updated',
                'timestamp': datetime.now().isoformat()
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
    
if __name__ == "__main__":
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    agent = SystemMonitorAgent(config)
    asyncio.run(agent.start())