import psutil
import platform
import asyncio
from typing import Dict, Any
import time

class SystemCollector:
    def __init__(self):
        self.last_cpu_times = None
        self.last_check = None

    async def get_cpu_metrics(self) -> Dict[str, Any]:
        """Collect CPU metrics"""
        try:
            # Get CPU usage percentage
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # Get CPU times
            cpu_times = psutil.cpu_times()
            
            # Get load average (Unix-like systems)
            load_avg = [0, 0, 0]
            if hasattr(psutil, 'getloadavg'):
                load_avg = psutil.getloadavg()
            
            # Get CPU frequency
            cpu_freq = psutil.cpu_freq()
            freq_current = cpu_freq.current if cpu_freq else 0
            
            # Get context switches and interrupts
            cpu_stats = psutil.cpu_stats()
            
            return {
                'usage_percent': round(cpu_percent, 2),
                'load_avg_1m': round(load_avg[0], 2),
                'load_avg_5m': round(load_avg[1], 2),
                'load_avg_15m': round(load_avg[2], 2),
                'frequency_mhz': round(freq_current, 2),
                'core_count': psutil.cpu_count(),
                'context_switches': cpu_stats.ctx_switches,
                'interrupts': cpu_stats.interrupts,
                'user_time': cpu_times.user,
                'system_time': cpu_times.system,
                'idle_time': cpu_times.idle
            }
        except Exception as e:
            return {'error': str(e), 'usage_percent': 0}

    async def get_memory_metrics(self) -> Dict[str, Any]:
        """Collect memory metrics"""
        try:
            # Virtual memory
            vmem = psutil.virtual_memory()
            
            # Swap memory
            swap = psutil.swap_memory()
            
            return {
                'total_mb': round(vmem.total / (1024 * 1024), 2),
                'available_mb': round(vmem.available / (1024 * 1024), 2),
                'used_mb': round(vmem.used / (1024 * 1024), 2),
                'usage_percent': round(vmem.percent, 2),
                'free_mb': round(vmem.free / (1024 * 1024), 2),
                'cached_mb': round(getattr(vmem, 'cached', 0) / (1024 * 1024), 2),
                'buffers_mb': round(getattr(vmem, 'buffers', 0) / (1024 * 1024), 2),
                'swap_total_mb': round(swap.total / (1024 * 1024), 2),
                'swap_used_mb': round(swap.used / (1024 * 1024), 2),
                'swap_usage_percent': round(swap.percent, 2)
            }
        except Exception as e:
            return {'error': str(e), 'usage_percent': 0}
