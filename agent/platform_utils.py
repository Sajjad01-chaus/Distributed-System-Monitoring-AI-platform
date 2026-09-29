import asyncio
import platform
import time
from typing import Any, Dict, List

import psutil


class PlatformUtils:
    async def get_uptime(self) -> int:
        return int(time.time() - psutil.boot_time())

    async def get_load_average(self) -> list:
        if platform.system() != "Windows":
            return list(psutil.getloadavg())
        return [0, 0, 0]

    async def get_service_status(self, service: str) -> str:
        """Return 'running', 'stopped' or 'not_found' for a named service/process."""
        if platform.system() == "Windows":
            try:
                return psutil.win_service_get(service).status()
            except psutil.NoSuchProcess:
                return "not_found"

        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", service,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        state = stdout.decode().strip()
        if state == "active":
            return "running"
        return "stopped" if state in ("inactive", "failed") else "not_found"

    async def get_disk_errors(self) -> List[Dict[str, Any]]:
        """Disks whose per-disk IO counters could not be read (a cheap health proxy)."""
        errors = []
        try:
            counters = psutil.disk_io_counters(perdisk=True) or {}
        except Exception as e:
            return [{"disk": "*", "error": str(e)}]
        for part in psutil.disk_partitions():
            try:
                psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError) as e:
                errors.append({"disk": part.device, "error": str(e)})
        if not counters:
            errors.append({"disk": "*", "error": "no disk IO counters available"})
        return errors
