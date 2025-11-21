import psutil
import asyncio
import subprocess
import time
from typing import Dict, Any, List
import socket
import platform

class NetworkCollector:
    def __init__(self):
        self.last_net_io = None
        self.last_check = None

    async def get_network_metrics(self) -> Dict[str, Any]:
        """Collect network metrics"""
        try:
            # Network IO statistics
            net_io = psutil.net_io_counters()
            current_time = time.time()
            
            metrics = {
                'bytes_sent_total': net_io.bytes_sent,
                'bytes_recv_total': net_io.bytes_recv,
                'packets_sent_total': net_io.packets_sent,
                'packets_recv_total': net_io.packets_recv,
                'errors_in': net_io.errin,
                'errors_out': net_io.errout,
                'drops_in': net_io.dropin,
                'drops_out': net_io.dropout
            }
            
            # Calculate rates if we have previous data
            if self.last_net_io and self.last_check:
                time_delta = current_time - self.last_check
                if time_delta > 0:
                    metrics.update({
                        'bytes_sent_per_sec': round((net_io.bytes_sent - self.last_net_io.bytes_sent) / time_delta, 2),
                        'bytes_recv_per_sec': round((net_io.bytes_recv - self.last_net_io.bytes_recv) / time_delta, 2),
                        'packets_sent_per_sec': round((net_io.packets_sent - self.last_net_io.packets_sent) / time_delta, 2),
                        'packets_recv_per_sec': round((net_io.packets_recv - self.last_net_io.packets_recv) / time_delta, 2)
                    })
            else:
                metrics.update({
                    'bytes_sent_per_sec': 0,
                    'bytes_recv_per_sec': 0,
                    'packets_sent_per_sec': 0,
                    'packets_recv_per_sec': 0
                })
            
            # Network connections
            connections = psutil.net_connections()
            metrics['connections_count'] = len(connections)
            metrics['established_connections'] = len([c for c in connections if c.status == 'ESTABLISHED'])
            
            # Network interfaces
            interfaces = await self.get_interface_stats()
            metrics['interfaces'] = interfaces
            
            # Network latency (ping to common servers)
            latency = await self.measure_latency()
            metrics.update(latency)
            
            self.last_net_io = net_io
            self.last_check = current_time
            
            return metrics
        except Exception as e:
            return {'error': str(e)}

    async def get_interface_stats(self) -> List[Dict[str, Any]]:
        """Get network interface statistics"""
        try:
            interfaces = []
            net_if_stats = psutil.net_if_stats()
            net_if_addrs = psutil.net_if_addrs()
            
            for interface, stats in net_if_stats.items():
                if_info = {
                    'name': interface,
                    'is_up': stats.isup,
                    'duplex': stats.duplex.name if hasattr(stats.duplex, 'name') else str(stats.duplex),
                    'speed': stats.speed,
                    'mtu': stats.mtu,
                    'addresses': []
                }
                
                # Get IP addresses
                if interface in net_if_addrs:
                    for addr in net_if_addrs[interface]:
                        if_info['addresses'].append({
                            'family': addr.family.name if hasattr(addr.family, 'name') else str(addr.family),
                            'address': addr.address,
                            'netmask': addr.netmask,
                            'broadcast': addr.broadcast
                        })
                
                interfaces.append(if_info)
            
            return interfaces
        except Exception as e:
            return [{'error': str(e)}]

    async def measure_latency(self) -> Dict[str, float]:
        """Measure network latency to common servers"""
        try:
            test_hosts = ['8.8.8.8', '1.1.1.1']
            latencies = {}
            
            for host in test_hosts:
                latency = await self.ping_host(host)
                latencies[f'latency_{host.replace(".", "_")}'] = latency
            
            # Average latency
            valid_latencies = [v for v in latencies.values() if v > 0]
            latencies['latency_ms'] = round(sum(valid_latencies) / len(valid_latencies), 2) if valid_latencies else 0
            
            return latencies
        except Exception as e:
            return {'latency_ms': 0, 'error': str(e)}

    async def ping_host(self, host: str, timeout: int = 3) -> float:
        """Ping a host and return latency in milliseconds"""
        try:
            system = platform.system().lower()
            
            if system == 'windows':
                cmd = ['ping', '-n', '1', '-w', str(timeout * 1000), host]
            else:
                cmd = ['ping', '-c', '1', '-W', str(timeout), host]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 1)
            
            if process.returncode == 0:
                output = stdout.decode()
                
                # Parse latency from ping output
                if system == 'windows':
                    if 'time=' in output:
                        latency_str = output.split('time=')[1].split('ms')[0]
                        return float(latency_str)
                else:
                    if 'time=' in output:
                        latency_str = output.split('time=')[1].split(' ms')[0]
                        return float(latency_str)
            
            return -1  # Ping failed
            
        except Exception:
            return -1