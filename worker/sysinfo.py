"""What this machine is doing right now, for the node card.

Everything here is best-effort and nothing raises. A node that can't
answer one of these questions reports the rest and leaves that one out;
a node that can't answer any of them reports nothing and its card looks
exactly as it did before. That matters because this is read on a
heartbeat every twenty seconds — a collector that throws would take the
heartbeat with it and the node would go offline for a decoration.

psutil is optional on purpose. It is the only sane way to read CPU and
memory the same way on Windows, macOS and Linux, and the alternative is
three lots of platform code — including Windows ctypes calls written on
a Mac, which is the one platform that actually matters here and the one
that could not be tested. So it is used when present and simply absent
when not: an older worker that hasn't reinstalled its requirements keeps
working and shows no stats, rather than failing to start.
"""

import shutil
import subprocess

try:
    import psutil
except ImportError:                # pragma: no cover - depends on install
    psutil = None

# nvidia-smi is asked once and remembered. Looking for it on every
# heartbeat costs a process spawn every twenty seconds, forever, on the
# machines that don't have it.
_NVIDIA = None


def _have_nvidia_smi():
    global _NVIDIA
    if _NVIDIA is None:
        _NVIDIA = shutil.which("nvidia-smi") or ""
    return _NVIDIA


def _cpu_and_memory():
    if not psutil:
        return {}
    out = {}
    try:
        # interval=None returns the load since the last call rather than
        # blocking. The first call after start reports 0.0, which is why
        # collect() is called on the heartbeat and not once at startup.
        out["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
        out["cpu_cores"] = psutil.cpu_count(logical=False) or None
        out["cpu_threads"] = psutil.cpu_count(logical=True) or None
    except Exception:
        pass
    try:
        mem = psutil.virtual_memory()
        out["memory_total"] = mem.total
        out["memory_used"] = mem.total - mem.available
        out["memory_percent"] = round(mem.percent, 1)
    except Exception:
        pass
    return out


def _gpus():
    """Every NVIDIA card, via nvidia-smi.

    Only NVIDIA: AMD and Intel have no equivalent that is present by
    default on a machine that hasn't gone looking for one, and inventing
    a half-answer for them would be worse than saying nothing. A machine
    with no NVIDIA card reports no GPUs and its card shows CPU and memory
    alone.
    """
    smi = _have_nvidia_smi()
    if not smi:
        return []
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                  "temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10)
        if out.returncode != 0:
            return []
    except (OSError, subprocess.TimeoutExpired):
        return []

    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue

        def number(text):
            try:
                return int(float(text))
            except (TypeError, ValueError):
                return None

        gpus.append({
            "name": parts[0],
            "percent": number(parts[1]),
            # nvidia-smi reports these in MiB with nounits.
            "memory_used": (number(parts[2]) or 0) * 1024 * 1024,
            "memory_total": (number(parts[3]) or 0) * 1024 * 1024,
            "temperature": number(parts[4]),
        })
    return gpus


def collect():
    """A snapshot for the node card. Never raises, may be empty."""
    stats = {}
    try:
        stats.update(_cpu_and_memory())
    except Exception:
        pass
    try:
        gpus = _gpus()
        if gpus:
            stats["gpus"] = gpus
    except Exception:
        pass
    return stats
