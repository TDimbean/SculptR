from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .protocol import contained_path


class ResourceSampler:
    def __init__(self, models_root: Path | None = None):
        self._previous_cpu = self._cpu_times()
        self._models_root = models_root
        self._storage_cache: tuple[float, dict[str, Any]] | None = None

    def sample(self) -> dict[str, Any]:
        cpu = self._cpu_percent()
        ram_used, ram_total = self._memory()
        gpu, vram_used, vram_total = self._nvidia()
        result = {
            "schema": 1,
            "updated_at": time.time(),
            "cpu_percent": cpu,
            "ram_used_bytes": ram_used,
            "ram_total_bytes": ram_total,
            "gpu_percent": gpu,
            "vram_used_bytes": vram_used,
            "vram_total_bytes": vram_total,
        }
        if self._models_root is not None:
            result["storage"] = self._storage()
        return result

    def _storage(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._storage_cache is not None and now - self._storage_cache[0] < 10.0:
            return self._storage_cache[1]
        used = 0
        stack = [self._models_root]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        used += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        try:
            total = shutil.disk_usage(self._models_root).total
        except OSError:
            total = None
        value = {"path": str(self._models_root), "used": used, "total": total}
        self._storage_cache = (now, value)
        return value

    def _cpu_percent(self) -> float | None:
        if sys.platform == "darwin":
            try:
                return round(min(100.0, os.getloadavg()[0] / max(1, os.cpu_count() or 1) * 100), 1)
            except OSError:
                return None
        current = self._cpu_times()
        previous = self._previous_cpu
        self._previous_cpu = current
        if current is None or previous is None:
            return None
        idle_delta = current[0] - previous[0]
        total_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)

    @staticmethod
    def _cpu_times() -> tuple[int, int] | None:
        if sys.platform == "win32":
            idle = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            return idle.value, kernel.value + user.value
        if sys.platform.startswith("linux"):
            try:
                values = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
                ticks = [int(value) for value in values]
            except (OSError, ValueError, IndexError):
                return None
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            return idle, sum(ticks)
        try:
            load = os.getloadavg()[0]
        except (AttributeError, OSError):
            return None
        total = max(1, os.cpu_count() or 1) * 100
        busy = min(total, round(load * 100))
        return total - busy, total

    @staticmethod
    def _memory() -> tuple[int | None, int | None]:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None, None
            return status.total_physical - status.available_physical, status.total_physical
        if sys.platform.startswith("linux"):
            try:
                rows = {}
                for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                    key, value = line.split(":", 1)
                    rows[key] = int(value.split()[0]) * 1024
                total = rows["MemTotal"]
                return total - rows["MemAvailable"], total
            except (OSError, KeyError, ValueError):
                return None, None
        return None, None

    @staticmethod
    def _nvidia() -> tuple[float | None, int | None, int | None]:
        executable = shutil.which("nvidia-smi")
        if not executable:
            return None, None, None
        try:
            result = subprocess.run(
                [
                    executable,
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
                shell=False,
            )
            values = [value.strip() for value in result.stdout.splitlines()[0].split(",")]
            return float(values[0]), int(values[1]) * 1_048_576, int(values[2]) * 1_048_576
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None, None, None


def clear_cache(models_root: Path) -> None:
    control = models_root.resolve() / ".sculptr"
    if control.is_symlink():
        raise ValueError("SculptR control directory cannot be a symlink")
    cache_entry = control / "cache"
    if cache_entry.is_symlink():
        cache_entry.unlink()
        cache_entry.mkdir(parents=True, exist_ok=True)
        return
    cache = contained_path(models_root, ".sculptr/cache")
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)
