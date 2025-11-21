import psutil
from typing import Dict, Any, List
import operator

class ProcessCollector:
    def __init__(self):
        pass

    async def get_top_processes(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get top processes by CPU and memory usage"""
        try:
            processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'create_time', 'username']):
                try:
                    pinfo = proc.info
                    pinfo['memory_mb'] = round(pinfo['memory_info'].rss / (1024 * 1024), 2) if pinfo['memory_info'] else 0
                    processes.append(pinfo)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            
            # Sort by CPU usage and get top processes
            top_cpu = sorted(processes, key=operator.itemgetter('cpu_percent'), reverse=True)[:limit]
            
            # Sort by memory usage and get top processes
            top_memory = sorted(processes, key=operator.itemgetter('memory_percent'), reverse=True)[:limit]
            
            return {
                'top_cpu': [{
                    'pid': p['pid'],
                    'name': p['name'],
                    'cpu_percent': p['cpu_percent'],
                    'memory_mb': p['memory_mb'],
                    'status': p['status'],
                    'username': p.get('username', 'unknown')
                } for p in top_cpu],
                'top_memory': [{
                    'pid': p['pid'],
                    'name': p['name'],
                    'cpu_percent': p['cpu_percent'],
                    'memory_mb': p['memory_mb'],
                    'memory_percent': p['memory_percent'],
                    'status': p['status'],
                    'username': p.get('username', 'unknown')
                } for p in top_memory],
                'total_processes': len(processes)
            }
            
        except Exception as e:
            return {'error': str(e), 'top_cpu': [], 'top_memory': []}

    async def get_process_details(self, pid: int) -> Dict[str, Any]:
        """Get detailed information about a specific process"""
        try:
            proc = psutil.Process(pid)
            
            return {
                'pid': proc.pid,
                'name': proc.name(),
                'exe': proc.exe(),
                'cmdline': proc.cmdline(),
                'status': proc.status(),
                'username': proc.username(),
                'create_time': proc.create_time(),
                'cpu_percent': proc.cpu_percent(),
                'memory_percent': proc.memory_percent(),
                'memory_info': proc.memory_info()._asdict(),
                'connections': len(proc.connections()),
                'num_threads': proc.num_threads(),
                'nice': proc.nice()
            }
            
        except Exception as e:
            return {'error': str(e)}

    async def kill_high_cpu_processes(self, cpu_threshold: float = 90.0, exclude_pids: List[int] = None) -> List[Dict[str, Any]]:
        """Kill processes consuming high CPU (for auto-remediation)"""
        try:
            if exclude_pids is None:
                exclude_pids = [os.getpid()]  # Don't kill ourselves
            
            killed_processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent']):
                try:
                    if proc.info['pid'] in exclude_pids:
                        continue
                        
                    if proc.info['cpu_percent'] > cpu_threshold:
                        proc.kill()
                        killed_processes.append({
                            'pid': proc.info['pid'],
                            'name': proc.info['name'],
                            'cpu_percent': proc.info['cpu_percent']
                        })
                        
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            return killed_processes
            
        except Exception as e:
            return [{'error': str(e)}]