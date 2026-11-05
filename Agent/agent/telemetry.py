# -*- coding: utf-8 -*-
"""Метрики узла: cgroup, диск, сеть, load average.

Агент живёт в контейнере, поэтому «железо» читается из cgroup, а не из
/proc целиком: так видны лимиты контейнера, а не мощность всего хоста.
Если cgroup недоступна (например, запуск на Windows при разработке),
соответствующие поля равны None — это нормально.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from .storage import Storage

__all__ = ["Telemetry"]

_CGROUP = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class Telemetry:
    """Снимок состояния узла для heartbeat и интерфейса."""

    def __init__(self, storage: Storage, started_at: float | None = None) -> None:
        self.storage = storage
        self.started_at = started_at if started_at is not None else time.monotonic()
        self._cpu_usec: int | None = self._cpu_usage_usec()
        self._cpu_ts = time.monotonic()
        self._net: tuple[int, int] | None = self._net_bytes()
        self._net_ts = time.monotonic()

    # -- cgroup ---------------------------------------------------------
    def _cpu_usage_usec(self) -> int | None:
        raw = _read(_CGROUP / "cpu.stat")
        if raw:
            for line in raw.splitlines():
                if line.startswith("usage_usec"):
                    try:
                        return int(line.split()[1])
                    except (IndexError, ValueError):
                        return None
        # cgroup v1: наносекунды
        value = _read_int(_CGROUP / "cpuacct" / "cpuacct.usage")
        if value is not None:
            return value // 1000
        return None

    def _cpu_limit(self) -> float | None:
        raw = _read(_CGROUP / "cpu.max")
        if raw:
            parts = raw.split()
            if len(parts) == 2 and parts[0] != "max":
                try:
                    return round(int(parts[0]) / int(parts[1]), 2)
                except (ValueError, ZeroDivisionError):
                    return None
            return None
        quota = _read_int(_CGROUP / "cpu" / "cpu.cfs_quota_us")
        period = _read_int(_CGROUP / "cpu" / "cpu.cfs_period_us")
        if quota and period and quota > 0:
            return round(quota / period, 2)
        return None

    def _memory(self) -> tuple[int | None, int | None]:
        current = _read_int(_CGROUP / "memory.current")
        limit = _read_int(_CGROUP / "memory.max")
        if current is None:
            current = _read_int(_CGROUP / "memory" / "memory.usage_in_bytes")
        if limit is None:
            limit = _read_int(_CGROUP / "memory" / "memory.limit_in_bytes")
        if limit is not None and limit > 2 ** 60:
            limit = None  # «без лимита» в cgroup v1
        return current, limit

    # -- сеть -----------------------------------------------------------
    def _net_bytes(self) -> tuple[int, int] | None:
        try:
            with open("/proc/net/dev", encoding="utf-8") as handle:
                rx = tx = 0
                for line in handle:
                    if ":" not in line:
                        continue
                    name, stats = line.split(":", 1)
                    if name.strip() == "lo":
                        continue
                    fields = stats.split()
                    if len(fields) >= 9:
                        rx += int(fields[0])
                        tx += int(fields[8])
                return rx, tx
        except (OSError, ValueError):
            return None

    # -- снимок ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        usage = self._cpu_usage_usec()
        cpu_cores: float | None = None
        if usage is not None and self._cpu_usec is not None:
            elapsed = now - self._cpu_ts
            if elapsed > 0:
                cpu_cores = round(max(0, usage - self._cpu_usec) / 1_000_000 / elapsed, 3)
        self._cpu_usec = usage if usage is not None else self._cpu_usec
        self._cpu_ts = now

        net = self._net_bytes()
        rx_rate = tx_rate = None
        if net is not None and self._net is not None:
            elapsed = max(1e-6, now - self._net_ts)
            rx_rate = round(max(0, net[0] - self._net[0]) / elapsed)
            tx_rate = round(max(0, net[1] - self._net[1]) / elapsed)
        self._net = net if net is not None else self._net
        self._net_ts = now

        try:
            disk = shutil.disk_usage(self.storage.root)
            disk_total, disk_free, disk_used = disk.total, disk.free, disk.used
        except OSError:
            disk_total = disk_free = disk_used = None

        memory, memory_limit = self._memory()

        load = None
        try:
            load = round(os.getloadavg()[0], 2)
        except (AttributeError, OSError):
            pass

        return {
            "cpu_cores": cpu_cores,
            "cpu_limit": self._cpu_limit(),
            "memory_bytes": memory,
            "memory_limit": memory_limit,
            "disk_total_bytes": disk_total,
            "disk_free_bytes": disk_free,
            "disk_used_bytes": disk_used,
            "load1": load,
            "net_rx_bps": rx_rate,
            "net_tx_bps": tx_rate,
            "uptime_seconds": round(now - self.started_at, 1),
        }
