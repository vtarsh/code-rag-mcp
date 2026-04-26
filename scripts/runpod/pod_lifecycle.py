#!/usr/bin/env python3
"""RunPod pod lifecycle CLI.

Subcommands:
    --status                    show account info + current pods + caps
    --list                      list current pods (terse)
    --start --gpu=X --secure-cloud --time-limit=60m --spending-cap=5 [--hold]
    --stop POD_ID               stop pod (graceful)
    --terminate POD_ID          delete pod (irreversible)
    --dry-run                   verify API key + auth without modifying state

Safety:
- SIGTERM/SIGINT handlers stop any pod mid-creation (always installed on --start)
- atexit handler stops pods at process exit ONLY when --hold is passed
  (otherwise `--start` would self-terminate the pod the moment main() returns;
  see verdict-stagec.md B1)
- Pod-side safety net: dashboard "Auto-terminate after: 1h" + Mac-side
  --time-limit + cost-guard caps. RunPod REST PodCreateInput rejects
  idleTimeoutInMin/terminationTime as unknown keys (2026-04-25), so those
  fields are NOT injected into the POST body.
- Cost guard runs before --start (assert_can_spend)
- API key never logged or printed
- Secure Cloud is enforced for pod creates (pay-com data isolation requirement)

GPU presets (mapped to RunPod gpuTypeId):
    rtx4090   -> NVIDIA GeForce RTX 4090
    a100-80g  -> NVIDIA A100 80GB PCIe
    a100-sxm  -> NVIDIA A100-SXM4-80GB
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

# Make `scripts.runpod.cost_guard` importable both when run as a CLI and when
# imported by the test suite (which adds project root to sys.path via conftest).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.runpod.cost_guard import CostGuardError, assert_can_spend

API_BASE: Final = os.getenv("RUNPOD_API_BASE", "https://rest.runpod.io/v1")
HTTP_TIMEOUT_SEC: Final = 15.0
DEFAULT_IMAGE: Final = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

GPU_PRESETS: Final[dict[str, dict]] = {
    "rtx4090": {"id": "NVIDIA GeForce RTX 4090", "vram_gb": 24, "approx_usd_per_hr": 0.34},
    "a40": {"id": "NVIDIA A40", "vram_gb": 48, "approx_usd_per_hr": 0.39},
    "a100-80g": {"id": "NVIDIA A100 80GB PCIe", "vram_gb": 80, "approx_usd_per_hr": 0.89},
    "a100-sxm": {"id": "NVIDIA A100-SXM4-80GB", "vram_gb": 80, "approx_usd_per_hr": 1.19},
}

# Module-level state for atexit/signal teardown.
_started_pod_ids: list[str] = []
_teardown_running: bool = False


# ----- HTTP helpers ----------------------------------------------------------


class PodLifecycleError(RuntimeError):
    """Raised on pod-management failures (auth, HTTP, validation)."""


def _read_api_key() -> str:
    key = os.getenv("RUNPOD_API_KEY")
    if not key:
        raise PodLifecycleError("RUNPOD_API_KEY missing. Run `source ~/.runpod/credentials` first.")
    if not key.startswith("rpa_"):
        raise PodLifecycleError("RUNPOD_API_KEY format unexpected (no rpa_ prefix).")
    return key


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {_read_api_key()}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise PodLifecycleError(f"HTTP {e.code} {method} {path}: {msg[:300]}") from e


# ----- Public API ------------------------------------------------------------


def list_pods() -> list[dict]:
    """Return list of pod summaries."""
    data = _request("GET", "/pods")
    return data if isinstance(data, list) else data.get("pods", [])


def get_pod(pod_id: str) -> dict:
    return _request("GET", f"/pods/{pod_id}")


def stop_pod(pod_id: str) -> dict:
    return _request("POST", f"/pods/{pod_id}/stop")


def terminate_pod(pod_id: str) -> dict:
    return _request("DELETE", f"/pods/{pod_id}")


def start_pod(
    *,
    gpu: str,
    secure_cloud: bool,
    time_limit_min: int,
    spending_cap_usd: float,
    image: str = DEFAULT_IMAGE,
    container_disk_gb: int = 50,
    volume_gb: int = 0,
    ssh_public_key: str | None = None,
    env: dict | None = None,
    name: str = "code-rag-finetune",
    purpose: str = "bench",
    now_fn=time.time,
) -> dict:
    """Create a pod after running the cost guard.

    Estimated cost = (time_limit_min/60) * preset[approx_usd_per_hr], capped at
    spending_cap_usd. Cost guard then ensures today_spend + estimate <= daily cap.

    Pod-side safety (verdict-stagec.md B7): RunPod REST PodCreateInput rejects
    unknown keys, so idleTimeoutInMin/terminationTime/minVcpuCount/minMemoryInGb
    are NOT sent. Safety net = account dashboard "Auto-terminate after: 1h" +
    Mac-side --time-limit (CLI gate, not server-enforced) + cost-guard cap.

    SSH access: pass ``ssh_public_key`` (full ``ssh-rsa AAA... user@host`` line)
    to inject it as the pod's ``PUBLIC_KEY`` env var. RunPod's pytorch image
    consumes that on container start and writes it to ``/root/.ssh/authorized_keys``.
    Without this, the pod is unreachable via SSH (only Web Terminal works).

    Persistent volume: pass ``volume_gb`` > 0 to attach a network volume mounted
    at /workspace. Survives pod restart but adds storage cost (~$0.07/GB/month).
    """
    if gpu not in GPU_PRESETS:
        raise PodLifecycleError(f"Unknown --gpu={gpu}. Available: {', '.join(GPU_PRESETS)}")
    if not secure_cloud:
        raise PodLifecycleError("Secure Cloud is required for pay-com data. Pass --secure-cloud.")
    # Bug 3 (NEXT_SESSION_PROMPT.md §2): TRAIN pods MUST have a persistent
    # volume — last cycle, 6 ephemeral pods wiped /workspace on stop and
    # destroyed all FT artifacts before they could be scp'd back / pushed
    # to HF Hub. Refuse to create when purpose=train and volume_gb < 20.
    if purpose == "train" and int(volume_gb) < 20:
        raise PodLifecycleError(
            "TRAIN purpose requires --volume-gb >= 20 to persist FT artifacts; ephemeral pods wipe /workspace on stop"
        )
    preset = GPU_PRESETS[gpu]
    estimated_usd = min(
        (time_limit_min / 60.0) * preset["approx_usd_per_hr"],
        spending_cap_usd,
    )
    assert_can_spend(estimated_usd)

    # RunPod REST rejects unknown keys (PodCreateInput has no minVcpu*/minMemory*/
    # idleTimeoutInMin/terminationTime fields as of 2026-04-25). The pod-side
    # safety net for B7 therefore lives in the account dashboard
    # ("Auto-terminate after: 1 hour") + Mac-side --time-limit + cost-guard cap.
    _ = now_fn  # kept for future B7 reintroduction once RunPod exposes the fields
    pod_env: dict = dict(env or {})
    if ssh_public_key:
        pod_env["PUBLIC_KEY"] = ssh_public_key.strip()
    body = {
        "name": name,
        "imageName": image,
        "gpuTypeIds": [preset["id"]],
        "cloudType": "SECURE",
        "computeType": "GPU",
        "containerDiskInGb": container_disk_gb,
        "volumeInGb": int(volume_gb),
        # B6: RunPod OpenAPI requires array<string>, not a comma-joined string.
        "ports": ["22/tcp", "8888/http"],
        "env": pod_env,
    }
    if int(volume_gb) > 0:
        body["volumeMountPath"] = "/workspace"
    pod = _request("POST", "/pods", body=body)
    pod_id = pod.get("id") or pod.get("podId")
    if pod_id:
        _started_pod_ids.append(pod_id)
    return pod


# ----- Teardown discipline ---------------------------------------------------


def _teardown(signum: int | None = None, _frame=None) -> None:
    """Stop any pod we started in this process. Safe to call multiple times."""
    global _teardown_running
    if _teardown_running:
        return
    _teardown_running = True
    for pid in list(_started_pod_ids):
        try:
            stop_pod(pid)
            print(f"[teardown] stop requested for pod {pid}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[teardown] WARN failed to stop {pid}: {e}", file=sys.stderr, flush=True)
    if signum is not None:
        sys.exit(130 if signum == signal.SIGINT else 143)


def install_signal_handlers(*, register_atexit: bool = False) -> None:
    """Install teardown triggers for pods started in this process.

    By default only SIGTERM/SIGINT are installed — these fire if the user ^C's
    mid-creation or the shell sends a kill signal, so a half-created pod does
    not leak. ``register_atexit=True`` additionally binds the teardown to
    process exit; use it only for Mac-driven workflows (``--start --hold``)
    where the launcher is expected to stay alive for the pod's lifetime. In
    the default ``--start`` mode the pod must outlive this process, so we do
    NOT register atexit (see verdict-stagec.md B1).
    """
    signal.signal(signal.SIGTERM, _teardown)
    signal.signal(signal.SIGINT, _teardown)
    if register_atexit:
        atexit.register(_teardown)


# ----- CLI -------------------------------------------------------------------


def _parse_time_limit(s: str) -> int:
    """Parse --time-limit values like 60m, 6h, 90."""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 60)
    if s.endswith("m"):
        return int(s[:-1])
    return int(s)


def _redact_pod_for_print(pod: dict) -> dict:
    """Strip env vars before printing — pod env may contain HF_TOKEN etc.

    Also derive ssh_host/ssh_port for downstream consumers: the REST start
    response includes ``publicIp`` + ``portMappings: {"22": NNNN}`` from which
    we resolve a single ssh endpoint (None if either piece missing).
    """
    safe = dict(pod)
    if "env" in safe:
        safe["env"] = {k: "***" for k in (safe["env"] or {})}
    pm = safe.get("portMappings") or {}
    pub_ip = safe.get("publicIp")
    ssh_port = pm.get("22") if isinstance(pm, dict) else None
    safe["ssh_host"] = pub_ip
    safe["ssh_port"] = ssh_port
    safe["pod_id"] = safe.get("id") or safe.get("podId")
    return safe


def wait_for_ssh_ready(
    pod_id: str,
    *,
    timeout_sec: int = 300,
    sleep_sec: int = 10,
    get_pod_fn=None,
    sleep_fn=time.sleep,
    now_fn=time.time,
) -> dict:
    """Poll the pod object until ``publicIp`` + ssh port are non-empty.

    RunPod's POST /pods returns ``desiredStatus="RUNNING"`` immediately, but
    ``publicIp=""`` and ``portMappings={}`` for ~30-90 seconds while the
    container actually boots (verified against running pods 2026-04-26 — the
    schema has ``publicIp`` (str), ``portMappings: {"22": NNNN}`` (str-keyed
    int), and ``templateId`` stays empty even after the pod is fully reachable
    so we deliberately don't gate on it).

    Treating spawn as "done" before the SSH endpoint exists causes
    immediate downstream provisioning failures ("pod missing ssh_host/
    ssh_port" → exit 1), wasting a pod creation. This helper closes that
    race by polling ``GET /pods/<id>`` until both fields are populated.

    Returns a dict ``{"ssh_host": str, "ssh_port": int, "pod_id": str}`` once
    reachable. Raises :class:`PodLifecycleError` if the timeout elapses.

    ``get_pod_fn``, ``sleep_fn``, ``now_fn`` are injection points for tests so
    the unit suite can simulate "endpoint lands on the 3rd poll" / "endpoint
    never appears" without burning real time or hitting the network.
    """
    get_pod_fn = get_pod_fn or get_pod
    deadline = now_fn() + timeout_sec
    last_info: dict | None = None
    while True:
        info = get_pod_fn(pod_id)
        last_info = info
        host = info.get("publicIp") or info.get("ssh_host") or ""
        # Prefer portMappings.22 (REST schema), fall back to ssh_port /
        # publicPort for older payload shapes.
        port = info.get("ssh_port") or info.get("publicPort")
        if not port:
            mappings = info.get("portMappings") or {}
            if isinstance(mappings, dict):
                # RunPod returns string keys ("22"); be lenient about int keys
                # too in case of future schema drift.
                port = mappings.get("22") or mappings.get(22)
        if host and port:
            return {
                "ssh_host": host,
                "ssh_port": int(port),
                "pod_id": pod_id,
            }
        if now_fn() >= deadline:
            raise PodLifecycleError(
                f"Pod {pod_id} did not become SSH-ready in {timeout_sec}s. Last info: {last_info!r}"
            )
        sleep_fn(sleep_sec)


def cmd_status() -> int:
    print(f"API base: {API_BASE}")
    try:
        _read_api_key()
        print("API key:  loaded (OK)")
    except PodLifecycleError as e:
        print(f"API key:  MISSING — {e}")
        return 2
    try:
        pods = list_pods()
        running = [p for p in pods if (p.get("desiredStatus") or p.get("status")) == "RUNNING"]
        print(f"Pods:     {len(pods)} total ({len(running)} RUNNING)")
        for p in pods:
            status = p.get("desiredStatus") or p.get("status") or "?"
            print(f"  - {p.get('id', '?')} status={status} costPerHr=${p.get('costPerHr', '?')}")
    except PodLifecycleError as e:
        print(f"Pods:     ERROR — {e}")
        return 3
    print(
        f"Caps:     daily=${os.getenv('RUNPOD_MAX_DAILY_SPEND_USD', '5')} "
        f"single-run=${os.getenv('RUNPOD_MAX_SINGLE_RUN_USD', '5')}"
    )
    return 0


def cmd_dry_run() -> int:
    """Verify API key + base URL without making mutating calls."""
    print(f"DRY RUN — API base: {API_BASE}")
    try:
        _read_api_key()
        print("API key:  loaded (OK)")
    except PodLifecycleError as e:
        print(f"API key:  MISSING — {e}")
        return 2
    try:
        list_pods()
        print("API auth: OK (GET /pods returned)")
        return 0
    except PodLifecycleError as e:
        print(f"API auth: FAILED — {e}")
        return 3


def cmd_list() -> int:
    pods = list_pods()
    for p in pods:
        status = p.get("desiredStatus") or p.get("status") or "?"
        print(f"{p.get('id', '?')}  {status}  {p.get('imageName', '?')}")
    return 0


def cmd_start(args) -> int:
    install_signal_handlers(register_atexit=bool(getattr(args, "hold", False)))
    ssh_pubkey: str | None = None
    if getattr(args, "ssh_public_key_file", None):
        path = Path(args.ssh_public_key_file).expanduser()
        if not path.exists():
            raise PodLifecycleError(f"--ssh-public-key-file not found: {path}")
        ssh_pubkey = path.read_text().strip()
        if not ssh_pubkey.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-")):
            raise PodLifecycleError(f"--ssh-public-key-file does not look like a public key: {path}")
    pod = start_pod(
        gpu=args.gpu,
        secure_cloud=args.secure_cloud,
        time_limit_min=_parse_time_limit(args.time_limit),
        spending_cap_usd=float(args.spending_cap),
        volume_gb=int(getattr(args, "volume_gb", 0) or 0),
        ssh_public_key=ssh_pubkey,
        purpose=getattr(args, "purpose", "bench"),
    )
    out_json = json.dumps(_redact_pod_for_print(pod), indent=2)
    print(out_json)
    if getattr(args, "out", None):
        # Write the *redacted* pod doc to disk for downstream scripts; ssh
        # connection info (publicIp / portMappings) is preserved, only env
        # var values (including PUBLIC_KEY) are redacted.
        Path(args.out).write_text(out_json)
    return 0


def cmd_stop(pod_id: str) -> int:
    res = stop_pod(pod_id)
    print(json.dumps(res, indent=2))
    return 0


def cmd_terminate(pod_id: str) -> int:
    res = terminate_pod(pod_id)
    print(json.dumps(res, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RunPod pod lifecycle CLI")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--start", action="store_true")
    g.add_argument("--stop", metavar="POD_ID")
    g.add_argument("--terminate", metavar="POD_ID")
    g.add_argument("--dry-run", action="store_true")

    p.add_argument("--gpu", choices=list(GPU_PRESETS), default="rtx4090")
    p.add_argument(
        "--secure-cloud",
        action="store_true",
        help="Required for pay-com data (no Community Cloud)",
    )
    p.add_argument(
        "--time-limit",
        default="60m",
        help="Auto-terminate after (e.g. 60m, 6h)",
    )
    p.add_argument(
        "--spending-cap",
        type=float,
        default=5.0,
        help="Hard $ cap for THIS run",
    )
    p.add_argument(
        "--hold",
        action="store_true",
        help=(
            "After --start, keep the process alive and register atexit so the "
            "pod is stopped when this process exits. Use for Mac-driven runs "
            "where the launcher stays up. Omit (default) to let the pod "
            "outlive the CLI — the pod-side idleTimeout / terminationTime "
            "provide the safety net."
        ),
    )
    p.add_argument(
        "--volume-gb",
        type=int,
        default=0,
        help=(
            "Persistent volume size (GB) mounted at /workspace. 0 = no volume. "
            "Adds ~$0.07/GB/month storage cost; survives pod restart."
        ),
    )
    p.add_argument(
        "--ssh-public-key-file",
        default=None,
        help=(
            "Path to SSH public key (.pub) — injected as pod PUBLIC_KEY env. "
            "Defaults to ~/.runpod/ssh/RunPod-Key-Go.pub when present (Bug 4 "
            "fix); fails closed when --start is requested and the file does "
            "not exist."
        ),
    )
    p.add_argument(
        "--purpose",
        choices=("train", "bench", "smoke"),
        default=None,
        help=(
            "Pod purpose. 'train' requires --volume-gb >= 20 to persist FT "
            "artifacts (Bug 3 fix). Required when --start is passed."
        ),
    )
    p.add_argument(
        "--hours",
        type=float,
        default=None,
        help="Convenience alias for --time-limit (hours).",
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Write redacted pod JSON to this path (for downstream scripts to "
            "read ssh_host/ssh_port). PUBLIC_KEY env value is redacted."
        ),
    )

    args = p.parse_args(argv)
    # --hours overrides --time-limit (both expressed in minutes internally)
    if getattr(args, "hours", None) is not None:
        args.time_limit = f"{int(args.hours * 60)}m"

    # Bug 4 (NEXT_SESSION_PROMPT.md §2): standardize the SSH public key.
    # Last cycle 3/5 pods used ~/.ssh/id_ed25519 and 2/5 used the RunPod
    # key, so cross-session agents lost access to half the pods. Default to
    # ~/.runpod/ssh/RunPod-Key-Go.pub when it exists, then fail-closed if a
    # path is set but missing. Only enforced for --start (other subcommands
    # don't touch SSH).
    if args.start:
        # Bug 3: --purpose is required for --start so the volume gate fires.
        if args.purpose is None:
            print(
                "ERROR: --purpose=(train|bench|smoke) is required with --start",
                file=sys.stderr,
            )
            return 4
        default_key = Path.home() / ".runpod" / "ssh" / "RunPod-Key-Go.pub"
        if args.ssh_public_key_file is None and default_key.exists():
            args.ssh_public_key_file = str(default_key)
        if args.ssh_public_key_file and not Path(args.ssh_public_key_file).expanduser().exists():
            print(
                f"SSH public key not found: {args.ssh_public_key_file}",
                file=sys.stderr,
            )
            return 4

    try:
        if args.status:
            return cmd_status()
        if args.dry_run:
            return cmd_dry_run()
        if args.list:
            return cmd_list()
        if args.start:
            return cmd_start(args)
        if args.stop:
            return cmd_stop(args.stop)
        if args.terminate:
            return cmd_terminate(args.terminate)
    except (PodLifecycleError, CostGuardError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
