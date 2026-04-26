"""Live pod monitor — single source of truth instead of ad-hoc SSH polling.

Polls every live RunPod pod every POLL_INTERVAL seconds, captures
(GPU%, VRAM, power, /proc CPU time, /proc IO, container disk %), writes
to ``bench_runs/pod_status.jsonl`` and emits stdout alerts when a pod
looks stuck (GPU 0% > IDLE_ALERT_MIN, wchar flat > IDLE_ALERT_MIN, or
disk > DISK_ALERT_PCT).

Run this as a long-lived background process while a Run cycle is in
flight. Tail bench_runs/pod_status.jsonl or grep for "ALERT:" in
stdout for a single dashboard.

Usage:
    source ~/.runpod/credentials
    python3 scripts/runpod/pod_watcher.py
    python3 scripts/runpod/pod_watcher.py --interval=30 --idle=3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SSH_KEY = Path("~/.runpod/ssh/RunPod-Key-Go").expanduser()
OUT_LOG = REPO_ROOT / "bench_runs" / "pod_status.jsonl"

POLL_INTERVAL = 60  # seconds
IDLE_ALERT_MIN = 5  # alert if GPU 0% or wchar flat for this many minutes
DISK_ALERT_PCT = 80  # alert if container disk > this %

# Per-pod state across ticks: {pod_id: {"gpu_zero_since": ts_or_None,
# "wchar_flat_since": ts_or_None, "last_wchar": int}}
_state: dict[str, dict] = {}


def _runpod_pods() -> list[dict]:
    """List currently RUNNING pods via REST API."""
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        sys.exit("RUNPOD_API_KEY missing — source ~/.runpod/credentials")
    req = urllib.request.Request(
        "https://rest.runpod.io/v1/pods",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"[watcher] WARN runpod list failed: {e}", flush=True)
        return []
    items = data if isinstance(data, list) else data.get("pods", [])
    return [p for p in items if p.get("desiredStatus") == "RUNNING"]


def _ssh_probe(host: str, port: int) -> dict:
    """Read live state from a pod via one SSH call.

    Captures nvidia-smi + /proc IO/stat for the most-relevant Python
    process (training or build_docs_vectors). Tolerant of missing PIDs
    (idle pod returns None for proc fields).
    """
    script = (
        "PID=$(pgrep -f 'train_reranker_ce|train_docs_embedder|build_docs_vectors' | head -1); "
        "echo PID=$PID; "
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw --format=csv,noheader,nounits | head -1; "
        'if [ -n "$PID" ]; then '
        "  awk '/wchar/ {print \"WCHAR=\"$2}' /proc/$PID/io; "
        "  STAT=$(cat /proc/$PID/stat); "
        "  echo CPU_TICKS=$(echo $STAT | awk '{print $14+$15}'); "
        "  cat /proc/$PID/status | awk '/^State:/ {print \"STATE=\"$2}'; "
        "fi; "
        'df -P /workspace 2>/dev/null | awk \'NR==2 {gsub("%","",$5); print "DISK_PCT="$5}\'; '
    )
    try:
        cp = subprocess.run(
            [
                "ssh",
                "-i",
                str(SSH_KEY),
                "-p",
                str(port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "LogLevel=ERROR",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                f"root@{host}",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        return {"ssh_error": str(e)}
    if cp.returncode != 0:
        return {"ssh_error": cp.stderr.strip()[:200]}

    out: dict = {}
    for line in cp.stdout.splitlines():
        line = line.strip()
        if line.startswith("PID="):
            v = line.split("=", 1)[1]
            out["pid"] = int(v) if v.isdigit() else None
        elif line.startswith("WCHAR="):
            out["wchar"] = int(line.split("=", 1)[1])
        elif line.startswith("CPU_TICKS="):
            out["cpu_ticks"] = int(line.split("=", 1)[1])
        elif line.startswith("STATE="):
            out["proc_state"] = line.split("=", 1)[1]
        elif line.startswith("DISK_PCT="):
            out["disk_pct"] = int(line.split("=", 1)[1])
        elif "," in line and "MiB" in cp.stdout:  # nvidia-smi line
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                try:
                    out["gpu_pct"] = int(re.sub(r"\D", "", parts[0]))
                    out["vram_mib"] = int(re.sub(r"\D", "", parts[1]))
                    out["power_w"] = float(re.sub(r"[^\d.]", "", parts[2]))
                except Exception:
                    pass
    return out


def _check_alerts(pod_id: str, name: str, probe: dict, now: float, idle_min: int, disk_pct: int) -> list[str]:
    """Compare against per-pod state, emit alert lines, mutate state."""
    alerts: list[str] = []
    s = _state.setdefault(pod_id, {"gpu_zero_since": None, "wchar_flat_since": None, "last_wchar": -1})

    gpu = probe.get("gpu_pct")
    if gpu is not None:
        if gpu == 0:
            if s["gpu_zero_since"] is None:
                s["gpu_zero_since"] = now
            elif (now - s["gpu_zero_since"]) / 60 >= idle_min:
                alerts.append(f"GPU 0% > {idle_min}min")
        else:
            s["gpu_zero_since"] = None

    wchar = probe.get("wchar")
    if wchar is not None:
        if wchar == s["last_wchar"]:
            if s["wchar_flat_since"] is None:
                s["wchar_flat_since"] = now
            elif (now - s["wchar_flat_since"]) / 60 >= idle_min:
                alerts.append(f"wchar flat > {idle_min}min")
        else:
            s["wchar_flat_since"] = None
            s["last_wchar"] = wchar

    if (probe.get("disk_pct") or 0) >= disk_pct:
        alerts.append(f"disk {probe['disk_pct']}% >= {disk_pct}%")

    return alerts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--interval", type=int, default=POLL_INTERVAL)
    p.add_argument("--idle", type=int, default=IDLE_ALERT_MIN, help="alert if GPU/wchar idle this many min")
    p.add_argument("--disk", type=int, default=DISK_ALERT_PCT)
    p.add_argument("--once", action="store_true", help="single tick then exit (test)")
    args = p.parse_args()

    OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    print(f"[watcher] polling every {args.interval}s, log -> {OUT_LOG}", flush=True)

    while True:
        tick_ts = time.time()
        pods = _runpod_pods()
        for pod in pods:
            pod_id = pod["id"]
            name = pod.get("name", "?")
            ip = pod.get("publicIp")
            port = (pod.get("portMappings") or {}).get("22")
            if not ip or not port:
                continue
            probe = _ssh_probe(ip, int(port))
            row = {
                "ts": int(tick_ts),
                "pod_id": pod_id,
                "name": name,
                "cost_per_hr": pod.get("costPerHr"),
                **probe,
            }
            with OUT_LOG.open("a") as f:
                f.write(json.dumps(row) + "\n")

            alerts = _check_alerts(pod_id, name, probe, tick_ts, args.idle, args.disk)
            if alerts:
                print(
                    f"[{time.strftime('%H:%M:%S')}] ALERT {name} ({pod_id}): {'; '.join(alerts)}",
                    flush=True,
                )
            else:
                print(
                    f"[{time.strftime('%H:%M:%S')}] {name[:30]:30s} GPU={probe.get('gpu_pct', '?'):>3}% "
                    f"VRAM={probe.get('vram_mib', '?')}MiB disk={probe.get('disk_pct', '?')}%",
                    flush=True,
                )
        if args.once:
            return
        # Sleep accounting for poll wall-time
        elapsed = time.time() - tick_ts
        time.sleep(max(1, args.interval - elapsed))


if __name__ == "__main__":
    main()
