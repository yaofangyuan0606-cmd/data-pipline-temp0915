"""Background sampler for host resources (CPU, memory, load, this process, GPUs via nvidia-smi).

Kept in a ring buffer; the console draws sparklines from it. GPU sampling is skipped when nvidia-smi
is not on PATH (this Mac), so the same page works unchanged on a CUDA box.
"""
from __future__ import annotations

import collections
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


class SystemMonitor:
    def __init__(self, interval: float = 2.0, keep: int = 450):
        self.interval = interval
        self.samples: collections.deque = collections.deque(maxlen=keep)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.nvidia = shutil.which("nvidia-smi")
        self.proc = psutil.Process() if psutil else None
        if psutil:
            psutil.cpu_percent(interval=None)
            self.proc.cpu_percent(interval=None)

    # ------------------------------------------------------------------ sampling
    def _gpus(self) -> list[dict]:
        if not self.nvidia:
            return []
        try:
            out = subprocess.run([self.nvidia, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return []
        gpus = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                try:
                    gpus.append({"index": int(parts[0]), "name": parts[1], "util_pct": float(parts[2]), "mem_used_mb": float(parts[3]), "mem_total_mb": float(parts[4]), "temp_c": float(parts[5]) if len(parts) > 5 and parts[5] else None})
                except ValueError:
                    continue
        return gpus

    def sample(self) -> dict:
        d = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if psutil:
            vm = psutil.virtual_memory()
            d.update(cpu_pct=psutil.cpu_percent(interval=None), mem_pct=vm.percent, mem_used_gb=round(vm.used / 1e9, 2), mem_total_gb=round(vm.total / 1e9, 2), proc_cpu_pct=self.proc.cpu_percent(interval=None), proc_rss_mb=round(self.proc.memory_info().rss / 1e6, 1))
        try:
            d["load1"] = round(__import__("os").getloadavg()[0], 2)
        except OSError:
            pass
        d["gpus"] = self._gpus()
        return d

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.samples.append(self.sample())
            except Exception:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.samples.append(self.sample())
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="emqc-sysmon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, last: int = 150) -> dict:
        items = list(self.samples)[-last:]
        return {
            "available": psutil is not None,
            "gpu_available": bool(self.nvidia),
            "cpu_count": psutil.cpu_count() if psutil else None,
            "interval_s": self.interval,
            "latest": items[-1] if items else None,
            "samples": items,
        }


monitor = SystemMonitor()
