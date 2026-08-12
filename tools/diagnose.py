#!/usr/bin/env python3
"""
DAEX Host Systems Diagnostic — canonical systems check for Project Icarus.

Produces a formatted report covering:
  • CPU usage   (psutil)
  • Memory usage (psutil)
  • Disk usage   (psutil)
  • GPU utilization  (nvidia-smi, if available)
  • Simulation readiness signal (if available via shared state / telemetry)

Usage:
    python tools/diagnose.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    print("FATAL: psutil is required. Install with: pip install psutil", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _section(title: str, width: int = 72) -> None:
    """Print a formatted section header."""
    print()
    sep = "═" * width
    print(sep)
    print(f"  {title}")
    print(sep)


def _key_value(label: str, value: str, indent: int = 2) -> None:
    """Print a key-value pair with right-aligned label."""
    padded_label = f"{label}:".ljust(20)
    print(f"{' ' * indent}{padded_label} {value}")


def _run(cmd: list[str], timeout: int = 15) -> str | None:
    """Run a shell command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"  [warn] Command {cmd!r} failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_cpu() -> dict:
    """Return CPU usage summary via psutil."""
    info: dict = {}
    try:
        per_cpu = psutil.cpu_percent(interval=1, percpu=True)
        info["per_core"] = per_cpu
        info["overall"] = psutil.cpu_percent(interval=0)  # instantaneous
        freq = psutil.cpu_freq()
        if freq and freq.current:
            info["frequency_MHz"] = f"{freq.current:.0f}"
        info["logical_cores"] = psutil.cpu_count(logical=True)
        info["physical_cores"] = psutil.cpu_count(logical=False) or "?"
    except Exception as exc:
        info["error"] = str(exc)
    return info


def collect_memory() -> dict:
    """Return memory usage summary via psutil."""
    mem = psutil.virtual_memory()
    return {
        "total_gb": f"{mem.total / (1024 ** 3):.2f}",
        "available_gb": f"{mem.available / (1024 ** 3):.2f}",
        "used_gb": f"{(mem.total - mem.available) / (1024 ** 3):.2f}",
        "percent_used": f"{mem.percent}%",
    }


def collect_disk() -> dict:
    """Return disk usage summary via psutil for the root partition."""
    try:
        disk = psutil.disk_usage("/")
    except Exception as exc:
        return {"error": str(exc)}

    labels = ["total_gb", "used_gb", "free_gb"]
    values = [disk.total, disk.used, disk.free]
    result = {}
    for label, val in zip(labels, values):
        result[label] = f"{val / (1024 ** 3):.2f}"
    result["percent_used"] = f"{disk.percent}%"

    # I/O counters
    try:
        io = psutil.disk_io_counters()
        if io:
            result["read_mb"] = f"{io.read_bytes / (1024 ** 2):.1f}"
            result["write_mb"] = f"{io.write_bytes / (1024 ** 2):.1f}"
    except Exception:
        pass

    return result


def collect_gpu() -> dict | None:
    """Query GPU utilization via nvidia-smi JSON output."""
    # Try the newer --query-gpu / --format=csv/nofetch approach first,
    # then fall back to parsing nvidia-smi text.
    gpu_info: dict = {}

    # Attempt structured query (nvidia-smi ≥ 9.x)
    result = _run([
        "nvidia-smi",
        "--query-gpu=index,name,driver_version,memory.total,"
        "memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw",
        "--format=csv,nounits,noheader",
    ], timeout=10)

    if result:
        lines = [l.strip() for l in result.splitlines() if l.strip()]
        gpus = []
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            # index, name, driver, mem_total, mem_used, mem_free, util_gpu, temp, power
            gpu = {
                "index": parts[0] if len(parts) > 0 else "?",
                "name": parts[1] if len(parts) > 1 else "?",
                "driver_version": parts[2] if len(parts) > 2 else "?",
                "memory_total_gb": f"{int(parts[3]) / 1024:.1f}" if len(parts) > 3 and parts[3].isdigit() else "?",
                "memory_used_mb": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else "?",
                "utilization_gpu_pct": f"{int(parts[6])}%" if len(parts) > 6 and parts[6].isdigit() else "?",
                "temperature_C": f"{parts[7]}°C" if len(parts) > 7 else "?",
            }
            gpus.append(gpu)
        gpu_info["gpus"] = gpus

    # Fallback: plain text nvidia-smi
    if not gpu_info.get("gpus"):
        text_result = _run(["nvidia-smi"], timeout=10)
        if text_result:
            gpu_info["raw_output"] = text_result[:500]  # cap for readability

    return gpu_info if gpu_info else None


def collect_simulation_readiness() -> dict | None:
    """Check for a simulation readiness signal.

    Looks in several common locations:
      1. A dedicated flag file (e.g., /tmp/icarus_sim_ready or .sim_ready)
      2. Telemetry sidecar state directory
      3. A local JSON metrics file produced by the agent pipeline
    """
    candidates = [
        # Flag files
        Path("/tmp/icarus_sim_ready"),
        Path("/tmp/simulation_ready"),
        Path(".sim_ready"),

        # Sidecar / telemetry directories (relative to script location)
        Path("sidecar/telemetry_sidecar") / "state.json",
    ]

    # Also check common project-relative paths
    for rel in ["backend/metrics", "data", "storage"]:
        candidates.append(Path(rel) / "metrics.json")
        candidates.append(Path(rel) / "snapshot.json")

    results: list[dict] = []

    for path in candidates:
        if not path.exists():
            continue
        try:
            content = json.loads(path.read_text())
            readiness = (
                content.get("simulation_readiness")
                or content.get("readiness")
                or "unknown"
            )
            results.append({
                "source": str(path),
                "type": "json",
                "sim_readiness": readiness,
            })
        except (json.JSONDecodeError, OSError):
            # Maybe it's a simple flag file
            text = path.read_text().strip()
            results.append({
                "source": str(path),
                "type": "flag_file",
                "content": text,
            })

    if not results:
        return None

    # Summarize
    readiness_values = [r.get("sim_readiness") or r.get("content", "?") for r in results]
    latest = max(readiness_values, key=lambda v: str(v))  # prefer last-found
    return {
        "detected_sources": len(results),
        "sources": [r["source"] for r in results],
        "latest_readiness": latest,
        "all_signals": readiness_values,
    }


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

def format_report() -> str:
    """Assemble and return the full diagnostic report as a string."""
    width = 72
    lines: list[str] = []

    def emit(text: str = ""):
        lines.append(text)

    # Title banner
    sep = "═" * width
    emit(sep)
    emit("  DAEX HOST SYSTEMS DIAGNOSTIC REPORT")
    emit(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    emit(f"  Hostname:  {os.uname().nodename if hasattr(os, 'uname') else 'unknown'}")
    emit(f"  Python:    {sys.version.split()[0]}")
    emit(sep)

    # --- CPU ---
    _section("CPU")
    cpu = collect_cpu()
    for key in ("overall", "logical_cores", "physical_cores"):
        if key in cpu and not isinstance(cpu[key], list):
            _key_value(key.replace("_", " ").title(), str(cpu[key]))
    if "frequency_MHz" in cpu:
        _key_value("Frequency (current)", f"{cpu['frequency_MHz']} MHz")
    per_core = cpu.get("per_core")
    if isinstance(per_core, list) and len(per_core) > 0:
        cores_str = ", ".join(f"C{i}: {v}%" for i, v in enumerate(per_core))
        _key_value("Per-core (%)", cores_str)

    # --- Memory ---
    _section("Memory (RAM)")
    mem = collect_memory()
    for key in ("total_gb", "used_gb", "available_gb", "percent_used"):
        label = {
            "total_gb": "Total",
            "used_gb": "Used",
            "available_gb": "Available",
            "percent_used": "% Used",
        }[key]
        _key_value(label, f"{mem[key]} GB" if key != "percent_used" else mem[key])

    # --- Disk ---
    _section("Disk (/)")
    disk = collect_disk()
    for key in ("total_gb", "used_gb", "free_gb", "percent_used"):
        label = {
            "total_gb": "Total",
            "used_gb": "Used",
            "free_gb": "Free",
            "percent_used": "% Used",
        }[key]
        _key_value(label, f"{disk[key]} GB" if key != "percent_used" else disk[key])
    for extra_key in ("read_mb", "write_mb"):
        if extra_key in disk:
            label = "Read" if extra_key == "read_mb" else "Written"
            _key_value(f"{label} (MB)", str(disk[extra_key]))

    # --- GPU ---
    _section("GPU")
    gpu = collect_gpu()
    if gpu is None:
        emit("  No NVIDIA GPU detected or nvidia-smi unavailable.")
    else:
        gpus = gpu.get("gpus", [])
        if gpus:
            for i, g in enumerate(gpus):
                header = f"  GPU {g['index']}: {g['name']} (Driver: {g['driver_version']})"
                emit(header)
                _key_value("Memory", f"{g['memory_total_gb']} GB total, "
                           f"{g['memory_used_mb']} MB used")
                _key_value("Utilization", g["utilization_gpu_pct"])
                _key_value("Temperature", g["temperature_C"])
        else:
            emit(f"  [warn] nvidia-smi returned data but no GPU entries parsed.")

    # --- Simulation Readiness ---
    _section("Simulation Readiness")
    sim = collect_simulation_readiness()
    if sim is None:
        emit("  No simulation readiness signal found on this host.")
        emit("  (This is expected when running outside the agent pipeline.)")
    else:
        num_sources = sim.get("detected_sources", "?")
        _key_value("Sources detected", str(num_sources))
        for src in sim.get("sources", []):
            _key_value("  Path", src)
        latest = sim.get("latest_readiness", "?")
        color_map = {
            "green": "✅ GREEN — Ready",
            "yellow": "⚠️  YELLOW — Partially ready",
            "red": "❌ RED — Not ready",
        }
        status_label = color_map.get(str(latest).lower(), f"Unknown: {latest}")
        _key_value("Latest signal", status_label)

    # Footer
    emit()
    emit(sep)
    emit("  END OF DIAGNOSTIC REPORT")
    emit(sep)

    return "\n".join(lines)


def main() -> None:
    report = format_report()
    print(report)

    # Also write the raw JSON to a temp location for programmatic consumption
    try:
        json_dir = Path("data/diagnostic")
        json_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = json_dir / f"diagnostics_{timestamp}.json"

        # Build a JSON-serializable summary
        summary: dict = {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "cpu": collect_cpu(),
            "memory": collect_memory(),
            "disk": collect_disk(),
            "gpu": collect_gpu(),
            "simulation_readiness": collect_simulation_readiness(),
        }

        json_path.write_text(json.dumps(summary, indent=2, default=str))
    except Exception:
        pass  # non-critical


if __name__ == "__main__":
    main()
