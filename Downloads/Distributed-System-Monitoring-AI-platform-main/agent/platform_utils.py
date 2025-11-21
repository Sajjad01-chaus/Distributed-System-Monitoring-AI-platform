import asyncio
import platform
import subprocess
import time
import psutil

class PlatformUtils:
    async def get_uptime(self) -> int:
        return int(time.time() - psutil.boot_time())
    
    async def get_load_average(self) -> list:
        if platform.system() != "Windows":
            return list(psutil.getloadavg())
        return [0, 0, 0]
