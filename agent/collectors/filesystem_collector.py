import psutil
import os
from typing import Dict, Any, List
from ..agent import SystemMonitorAgent
import asyncio
import time
class FilesystemCollector:
    def __init__(self):
        pass

    async def get_disk_metrics(self) -> Dict[str, Any]:
        """Collect disk usage and IO metrics"""
        try:
            # Disk usage for all mounted filesystems
            disk_usage = []
            for partition in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    disk_usage.append({
                        'device': partition.device,
                        'mountpoint': partition.mountpoint,
                        'fstype': partition.fstype,
                        'total_gb': round(usage.total / (1024**3), 2),
                        'used_gb': round(usage.used / (1024**3), 2),
                        'free_gb': round(usage.free / (1024**3), 2),
                        'usage_percent': round((usage.used / usage.total) * 100, 2)
                    })
                except PermissionError:
                    continue
            
            # Overall disk usage (primary disk)
            primary_usage = disk_usage[0] if disk_usage else {'usage_percent': 0}
            
            # Disk IO statistics
            disk_io = psutil.disk_io_counters()
            
            metrics = {
                'usage_percent': primary_usage['usage_percent'],
                'total_gb': sum(d['total_gb'] for d in disk_usage),
                'used_gb': sum(d['used_gb'] for d in disk_usage),
                'free_gb': sum(d['free_gb'] for d in disk_usage),
                'partitions': disk_usage
            }
            
            if disk_io:
                metrics.update({
                    'read_bytes_total': disk_io.read_bytes,
                    'write_bytes_total': disk_io.write_bytes,
                    'read_count': disk_io.read_count,
                    'write_count': disk_io.write_count,
                    'read_time_ms': disk_io.read_time,
                    'write_time_ms': disk_io.write_time
                })
            
            return metrics
            
        except Exception as e:
            return {'error': str(e), 'usage_percent': 0}

    async def get_io_stats(self) -> Dict[str, Any]:
        """Get detailed IO statistics"""
        try:
            disk_io = psutil.disk_io_counters(perdisk=True)
            io_stats = {}
            
            for device, stats in disk_io.items():
                io_stats[device] = {
                    'read_bytes': stats.read_bytes,
                    'write_bytes': stats.write_bytes,
                    'read_count': stats.read_count,
                    'write_count': stats.write_count,
                    'read_time': stats.read_time,
                    'write_time': stats.write_time
                }
            
            return io_stats
            
        except Exception as e:
            return {'error': str(e)}

    async def get_mount_points(self) -> List[Dict[str, str]]:
        """Get all mount points"""
        try:
            mount_points = []
            for partition in psutil.disk_partitions():
                mount_points.append({
                    'device': partition.device,
                    'mountpoint': partition.mountpoint,
                    'fstype': partition.fstype,
                    'opts': partition.opts
                })
            
            return mount_points
            
        except Exception as e:
            return [{'error': str(e)}]

    async def cleanup_temp_files(self, temp_dirs: List[str] = None) -> Dict[str, Any]:
        """Clean up temporary files (for auto-remediation)"""
        try:
            if temp_dirs is None:
                temp_dirs = ['/tmp', '/var/tmp'] if os.name != 'nt' else ['C:\\Temp', 'C:\\Windows\\Temp']
            
            cleaned_files = 0
            freed_bytes = 0
            
            for temp_dir in temp_dirs:
                if not os.path.exists(temp_dir):
                    continue
                    
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        try:
                            # Only delete files older than 1 day
                            if os.path.getctime(file_path) < (time.time() - 86400):
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                cleaned_files += 1
                                freed_bytes += file_size
                        except (OSError, PermissionError):
                            continue
            
            return {
                'cleaned_files': cleaned_files,
                'freed_mb': round(freed_bytes / (1024 * 1024), 2),
                'success': True
            }
            
        except Exception as e:
            return {'error': str(e), 'success': False}

if __name__ == "__main__":
    import yaml
    
    # Load configuration
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    # Create and start agent
    agent = SystemMonitorAgent(config)
    asyncio.run(agent.start())