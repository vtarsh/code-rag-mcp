#!/usr/bin/env python3
"""Single-candidate end-to-end RunPod pipeline runner (Bug 5 fix).

Last cycle, 5/6 pods stopped externally because parallel orchestrator agents
treated "training is running, I'll come back later" as return-to-caller; the
caller then assumed the work was done and stopped pods, wiping ephemeral
volumes mid-build. The fix: this script OWNS one pod from spawn to stop, in a
single linear flow, and NEVER returns to the caller until either:

  (a) the bench JSON sentinel exists locally on Mac under
      /tmp/<candidate>_bench.json, OR
  (b) an explicit FAILED status is set with a non-zero exit code.

Steps (per NEXT_SESSION_PROMPT.md §4 + §5):

  1. spawn pod via pod_lifecycle (--purpose=train --volume-gb=20)
  2. 7-step smoke (load base, encode 3 docs, capture vec; train 100 steps;
     reload, encode same 3 docs; cos<0.999 verify; small index 100 rows;
     bench probe=10; verify model_key in JSON)
  3. full train (caller-supplied recipe args)
  4. full bench (writes /tmp/<candidate>_bench.json on Mac via scp-back)
  5. HF Hub push of FT artifact (model survives pod stop even if scp fails)
  6. stop pod
  7. poll-loop guarantee: until [bench JSON exists]; do sleep 60; done

Exit codes (per Bug 5 contract):

  0 = success, bench JSON on Mac
  1 = smoke failed (pod stopped, no further work)
  2 = train failed (pod stopped, partial logs scp'd back)
  3 = bench failed (pod stopped, train artifact pushed to HF anyway)
  4 = pod-orphan-detected (pod still running, requires manual intervention)

Heavy work (ssh / scp / runpod-API calls) is delegated to small thunk-style
helpers so the unit tests in tests/test_runpod_lifecycle.py can mock the
network surface and verify the contract (try/finally pod-stop, poll-until-
sentinel-exists, smoke-failure-skips-train).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# Make sibling helpers importable both as CLI and from the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.runpod import pod_lifecycle
from scripts.runpod.pod_lifecycle import PodLifecycleError

# ----- Constants -------------------------------------------------------------

DEFAULT_SSH_KEY: Final = Path.home() / ".runpod" / "ssh" / "RunPod-Key-Go"
DEFAULT_SSH_PUB: Final = DEFAULT_SSH_KEY.with_suffix(".pub")
DEFAULT_BENCH_DIR: Final = Path("/tmp")
SMOKE_TIME_LIMIT_MIN: Final = 180  # generous: smoke + train + bench fit in 3h
POLL_INTERVAL_SEC: Final = 60
POLL_MAX_WAIT_SEC: Final = 4 * 3600  # 4h hard ceiling (orphan detection)

# Local repo root (where scripts/ and src/ live) — used by the tar-overlay
# step to push our local Phase-0 working-tree on top of the pod's stale clone
# of origin/main. Resolved at import time so tests can monkeypatch it.
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
# Where setup_env.sh clones the repo on the pod. _make_*_command and the
# overlay step both use this so the pod sees our local-overlay scripts/+src/.
REMOTE_REPO_DIR: Final = "/workspace/code-rag-mcp"

# Exit codes ----------------------------------------------------------------
EXIT_OK: Final = 0
EXIT_SMOKE_FAILED: Final = 1
EXIT_TRAIN_FAILED: Final = 2
EXIT_BENCH_FAILED: Final = 3
EXIT_ORPHAN_DETECTED: Final = 4

# RunPod GPU alias resolution: the CLI accepts the friendly tags `4090`, `a40`,
# `a100`; pod_lifecycle.GPU_PRESETS uses `rtx4090`, `a40`, `a100-80g`.
GPU_ALIASES: Final[dict[str, str]] = {
    "4090": "rtx4090",
    "a40": "a40",
    "a100": "a100-80g",
}


# ----- Result container ------------------------------------------------------


@dataclass
class PipelineResult:
    """End-of-run summary returned by run_pipeline().

    Carrying the fields explicitly rather than mutating exit codes globally
    keeps run_pipeline() a pure-ish function the tests can call directly.
    """

    exit_code: int
    candidate_tag: str
    pod_id: str | None = None
    pod_stopped: bool = False
    bench_json_path: Path | None = None
    smoke_passed: bool = False
    train_pushed_to_hf: bool = False
    failure_step: str | None = None
    failure_reason: str | None = None
    log_lines: list[str] = field(default_factory=list)


# ----- Thin shell wrappers (mocked in tests) ---------------------------------


def _log(result: PipelineResult, msg: str) -> None:
    """Append to in-memory log AND emit to stderr so progress is visible to a
    parent agent reading the runner's tail."""
    line = f"[full_pipeline:{result.candidate_tag}] {msg}"
    result.log_lines.append(line)
    print(line, file=sys.stderr, flush=True)


def _ssh_run(
    *,
    host: str,
    port: int,
    cmd: str,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout_sec: int = 60 * 60,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Run a command on the pod via SSH. Returns CompletedProcess.

    `runner` is patchable from tests (default = subprocess.run). We never use
    `shell=True` — the remote command is one positional arg consumed by sshd's
    own shell. StrictHostKeyChecking=no because pods are ephemeral; warning
    spam is silenced via -o LogLevel=ERROR.
    """
    argv = [
        "ssh",
        "-i",
        str(key_path),
        "-p",
        str(port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        f"root@{host}",
        cmd,
    ]
    return runner(argv, capture_output=True, text=True, timeout=timeout_sec)


def _scp_back(
    *,
    host: str,
    port: int,
    remote_path: str,
    local_path: Path,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout_sec: int = 600,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Copy a file from pod -> Mac. `runner` is patchable from tests."""
    argv = [
        "scp",
        "-i",
        str(key_path),
        "-P",
        str(port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        f"root@{host}:{remote_path}",
        str(local_path),
    ]
    return runner(argv, capture_output=True, text=True, timeout=timeout_sec)


def _scp_to(
    *,
    host: str,
    port: int,
    local_path: Path,
    remote_path: str,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout_sec: int = 600,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Copy a file from Mac -> pod. `runner` is patchable from tests."""
    argv = [
        "scp",
        "-i",
        str(key_path),
        "-P",
        str(port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        str(local_path),
        f"root@{host}:{remote_path}",
    ]
    return runner(argv, capture_output=True, text=True, timeout=timeout_sec)


def _tar_overlay_to(
    *,
    host: str,
    port: int,
    repo_root: Path,
    local_dirs: list[str],
    remote_root: str,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout_sec: int = 600,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Stream `local_dirs` (relative to `repo_root`) from Mac into `remote_root`.

    Built with `tar cz | ssh ... 'cd <remote_root> && tar xz'` because rsync
    isn't installed on a fresh runpod/pytorch image. The pipe is realised by a
    single `bash -c` so we get one CompletedProcess back with combined stderr.
    Returns rc=0 on success; any non-zero rc means the overlay failed and the
    pod still has the stale `git clone` of origin/main, which would silently
    skip our Phase-0 fixes.

    `runner` is patchable so tests can verify the bash invocation without
    actually shelling out.
    """
    # Validate the local source dirs up front — a missing dir would be
    # silently ignored by tar with rc=0 + a warning; we need rc != 0 so the
    # caller maps it to provision-failed.
    for d in local_dirs:
        if not (repo_root / d).is_dir():
            return subprocess.CompletedProcess(
                args=[],
                returncode=2,
                stdout="",
                stderr=f"_tar_overlay_to: missing local dir {d!r} under {repo_root}",
            )
    # Single bash invocation pipes tar through ssh, so any failure (tar
    # error, ssh broken, remote tar nonzero) bubbles up via `set -o pipefail`.
    ssh_target = f"root@{host}"
    tar_cmd = (
        "set -o pipefail; "
        f"tar -czf - -C {str(repo_root)!r} {' '.join(local_dirs)} | "
        f"ssh -i {str(key_path)!r} -p {port} "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o LogLevel=ERROR {ssh_target} "
        f"'mkdir -p {remote_root} && cd {remote_root} && tar -xzf -'"
    )
    return runner(
        ["bash", "-c", tar_cmd],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


# ----- Pod lifecycle (delegate to pod_lifecycle.py) --------------------------


def _spawn_pod(
    *,
    candidate_tag: str,
    gpu: str,
    volume_gb: int = 20,
    time_limit_min: int = SMOKE_TIME_LIMIT_MIN,
    ssh_public_key_path: Path = DEFAULT_SSH_PUB,
    spending_cap_usd: float = 5.0,
    ssh_ready_timeout_sec: int = 300,
    ssh_ready_sleep_sec: int = 10,
) -> dict:
    """Spawn a TRAIN pod via pod_lifecycle.start_pod with Bug-3-fixed defaults.

    Returns the redacted pod dict with ``ssh_host``, ``ssh_port``, ``pod_id``
    guaranteed populated — we poll
    :func:`pod_lifecycle.wait_for_ssh_ready` after the create call because
    RunPod returns the pod object with ``publicIp=""`` / ``portMappings={}``
    for ~30-90 seconds while the container actually boots. Without the wait,
    every spawn raced into "pod missing ssh_host/ssh_port" → exit 1.

    Raises PodLifecycleError on failure (caller catches at orchestrator level).

    Forwards HF_TOKEN from the local environment as a pod env var so that
    setup_env.sh can `huggingface-cli login` non-interactively and so that
    `_make_hf_push_command`'s upload_folder call has credentials.
    """
    gpu_resolved = GPU_ALIASES.get(gpu, gpu)
    ssh_pubkey = None
    if ssh_public_key_path and Path(ssh_public_key_path).expanduser().exists():
        ssh_pubkey = Path(ssh_public_key_path).expanduser().read_text().strip()
    # HF_TOKEN propagation: setup_env.sh and the train/bench/push steps all
    # need a logged-in HF Hub. RunPod's start_pod accepts an `env` dict that
    # gets injected into the container at boot. _redact_pod_for_print will
    # mask the token before any pod dict gets logged.
    pod_env: dict = {}
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        pod_env["HF_TOKEN"] = hf_token
    pod = pod_lifecycle.start_pod(
        gpu=gpu_resolved,
        secure_cloud=True,
        time_limit_min=time_limit_min,
        spending_cap_usd=spending_cap_usd,
        volume_gb=int(volume_gb),
        ssh_public_key=ssh_pubkey,
        env=pod_env or None,
        purpose="train",
        name=f"pipeline-{candidate_tag}",
    )
    redacted = pod_lifecycle._redact_pod_for_print(pod)
    pod_id = redacted.get("pod_id") or pod.get("id") or pod.get("podId")
    if not pod_id:
        raise PodLifecycleError(f"start_pod returned no id; cannot poll for SSH readiness: {redacted!r}")
    # Race window: the POST response often has publicIp="" / portMappings={}
    # for ~30-90s while the container actually boots. Poll GET /pods/<id>
    # until both fields appear; raise PodLifecycleError on timeout so the
    # caller treats it as a spawn failure (not a silent provisioning hang).
    ssh = pod_lifecycle.wait_for_ssh_ready(
        pod_id,
        timeout_sec=ssh_ready_timeout_sec,
        sleep_sec=ssh_ready_sleep_sec,
    )
    redacted["ssh_host"] = ssh["ssh_host"]
    redacted["ssh_port"] = ssh["ssh_port"]
    redacted["pod_id"] = ssh["pod_id"]
    return redacted


def _stop_pod_safely(pod_id: str | None, result: PipelineResult) -> None:
    """Best-effort pod stop. Sets result.pod_stopped on success.

    Never raises — this runs in `finally` blocks where a raise would mask the
    real failure cause.
    """
    if not pod_id:
        return
    try:
        pod_lifecycle.stop_pod(pod_id)
        result.pod_stopped = True
        _log(result, f"pod {pod_id} stopped OK")
    except Exception as e:
        _log(result, f"WARN failed to stop pod {pod_id}: {e}")


def _verify_pod_stopped(pod_id: str | None, result: PipelineResult) -> bool:
    """Cross-check via pod_lifecycle.get_pod that the stop actually took.

    Returns True if pod is no longer in RUNNING state. Bug 5's pod-orphan
    contract relies on this — if a pod is still RUNNING when we exit, we set
    EXIT_ORPHAN_DETECTED so the caller knows to intervene.
    """
    if not pod_id:
        return True
    try:
        info = pod_lifecycle.get_pod(pod_id)
        status = (info.get("desiredStatus") or info.get("status") or "").upper()
        if status == "RUNNING":
            _log(result, f"WARN pod {pod_id} still RUNNING post-stop")
            return False
        return True
    except Exception as e:
        _log(result, f"WARN could not verify pod {pod_id} status: {e}")
        # When the API is flaky, default to "assume orphan" so we don't
        # silently leak a pod (the caller's manual sweep will catch it).
        return False


# ----- Provisioning (setup_env.sh + Phase-0 overlay) -------------------------


def _provision_pod(
    *,
    host: str,
    port: int,
    key_path: Path,
    repo_root: Path,
    result: PipelineResult,
    ssh_fn: Callable[..., subprocess.CompletedProcess],
    scp_to_fn: Callable[..., subprocess.CompletedProcess],
    tar_overlay_fn: Callable[..., subprocess.CompletedProcess],
) -> bool:
    """Bring a fresh runpod/pytorch image to a state our scripts can run in.

    Three sub-steps, all required for Phase 0 to actually exercise the local
    fixes (Bugs 1-5 + new eval builders) instead of stale origin/main:

      1. scp `setup_env.sh` over and run it: installs apt + pip deps, clones
         vtarsh/code-rag-mcp at origin/main into /workspace/code-rag-mcp/, and
         logs into HF Hub if HF_TOKEN is set in the pod env.
      2. tar-stream `scripts/` and `src/` from our local working tree into
         /workspace/code-rag-mcp/ on top of the clone — this is the
         "Phase 0 overlay" that gives the pod our latest unpushed code.
      3. sanity-check that `import scripts.benchmark_doc_intent` works from
         the overlay path; if it doesn't, the overlay silently no-op'd and
         later steps would silently run against stale code.

    Returns True on full success. On failure, sets result.failure_step =
    "provision" + result.failure_reason and returns False (caller maps to
    EXIT_SMOKE_FAILED + finally-block pod stop). Never raises.
    """
    # --- (a) push setup_env.sh ---------------------------------------------
    setup_local = repo_root / "scripts" / "runpod" / "setup_env.sh"
    if not setup_local.is_file():
        _log(result, f"FAIL provision: setup_env.sh missing at {setup_local}")
        result.failure_step = "provision"
        result.failure_reason = f"setup_env.sh not found at {setup_local}"
        return False
    cp = scp_to_fn(
        host=host,
        port=port,
        local_path=setup_local,
        remote_path="/workspace/setup_env.sh",
        key_path=key_path,
    )
    if cp.returncode != 0:
        _log(result, f"FAIL provision (scp setup_env.sh): rc={cp.returncode} stderr={(cp.stderr or '')[:300]}")
        result.failure_step = "provision"
        result.failure_reason = f"scp setup_env.sh rc={cp.returncode}: {(cp.stderr or '')[:200]}"
        return False

    # --- (b) run setup_env.sh on the pod -----------------------------------
    # HF_TOKEN should already be in the pod's process env via _spawn_pod's
    # `env=` kwarg; setup_env.sh reads it from there. We bash -lc it so the
    # pod's container init has fully populated /etc/environment by then.
    setup_cmd = "bash /workspace/setup_env.sh"
    cp = ssh_fn(
        host=host,
        port=port,
        cmd=setup_cmd,
        key_path=key_path,
        timeout_sec=20 * 60,  # apt-get + pip + git-clone can be slow
    )
    if cp.returncode != 0:
        _log(result, f"FAIL provision (setup_env.sh): rc={cp.returncode} stderr={(cp.stderr or '')[:300]}")
        result.failure_step = "provision"
        result.failure_reason = f"setup_env.sh rc={cp.returncode}: {(cp.stderr or '')[:200]}"
        return False

    # --- (c) tar-overlay our local Phase-0 scripts/ + src/ -----------------
    cp = tar_overlay_fn(
        host=host,
        port=port,
        repo_root=repo_root,
        local_dirs=["scripts", "src"],
        remote_root=REMOTE_REPO_DIR,
        key_path=key_path,
    )
    if cp.returncode != 0:
        _log(result, f"FAIL provision (tar overlay): rc={cp.returncode} stderr={(cp.stderr or '')[:300]}")
        result.failure_step = "provision"
        result.failure_reason = f"tar overlay rc={cp.returncode}: {(cp.stderr or '')[:200]}"
        return False

    # --- (d) sanity-check that the overlay actually landed -----------------
    # If `import scripts.benchmark_doc_intent` fails, our overlay didn't
    # take and a downstream `python3 scripts/benchmark_doc_intent.py` will
    # silently run against stale clone-only files (or, more likely, fail
    # late with an obscure traceback). Catching it here saves an entire pod
    # hour of wasted GPU time.
    sanity_cmd = (
        f"cd {REMOTE_REPO_DIR} && "
        "python3 -c \"import sys; sys.path.insert(0, '.'); "
        'import scripts.benchmark_doc_intent"'
    )
    cp = ssh_fn(
        host=host,
        port=port,
        cmd=sanity_cmd,
        key_path=key_path,
        timeout_sec=60,
    )
    if cp.returncode != 0:
        _log(result, f"FAIL provision (sanity): rc={cp.returncode} stderr={(cp.stderr or '')[:300]}")
        result.failure_step = "provision"
        result.failure_reason = f"sanity import scripts.benchmark_doc_intent failed: {(cp.stderr or '')[:200]}"
        return False

    _log(result, "provision OK (setup_env + overlay + sanity)")
    return True


# ----- Smoke / train / bench step helpers ------------------------------------


def _make_smoke_command(
    *,
    kind: str,
    candidate_tag: str,
    base_model: str,
    train_data_remote: str,
    eval_data_remote: str,
) -> str:
    """Return the bash one-liner that runs the 7-step smoke on the pod.

    Kept as a pure function so tests can assert the command contains the
    expected substrings (cos<0.999 check, probe=10, model_key verify) without
    actually executing it.
    """
    smoke_dir = f"/workspace/{candidate_tag}_smoke"
    smoke_json = f"/tmp/smoke_{candidate_tag}.json"
    base_vec_json = "/tmp/base_vec.json"
    if kind == "reranker":
        # For rerankers the smoke ends with --rerank-model-path probe.
        bench_step = (
            f"python3 scripts/benchmark_doc_intent.py "
            f"--eval={eval_data_remote} --model=docs --rerank-on "
            f"--rerank-model-path={smoke_dir} --probe=10 "
            f"--out={smoke_json} && "
            f"jq -e '.rerank_model | contains(\"{candidate_tag}_smoke\")' "
            f"{smoke_json}"
        )
    else:
        # Docs tower: build small index + bench probe=10 with model key.
        # CODE_RAG_HOME points at the cloned+overlaid repo (REMOTE_REPO_DIR),
        # not /workspace — that's where conventions.yaml + profile data live.
        bench_step = (
            f"CODE_RAG_HOME={REMOTE_REPO_DIR} python3 scripts/build_docs_vectors.py "
            f"--model={candidate_tag}-smoke --probe-rows=100 --force && "
            f"python3 scripts/benchmark_doc_intent.py "
            f"--eval={eval_data_remote} "
            f"--model={candidate_tag}-smoke --probe=10 --out={smoke_json} && "
            f"jq -e '.model_key == \"{candidate_tag}-smoke\"' {smoke_json}"
        )

    py_capture_base = (
        'python3 -c "'
        "from sentence_transformers import SentenceTransformer;"
        f" m = SentenceTransformer('{base_model}', trust_remote_code=True);"
        " v = m.encode(['hello world','refund flow','webhook'], "
        "normalize_embeddings=True);"
        " import json;"
        f" json.dump({{'shape': list(v.shape),"
        " 'norms': [float((x*x).sum()**0.5) for x in v]}},"
        f" open('{base_vec_json}', 'w'))"
        '"'
    )

    py_verify_delta = (
        'python3 -c "'
        "from sentence_transformers import SentenceTransformer;"
        " import json;"
        f" m = SentenceTransformer('{smoke_dir}', trust_remote_code=True);"
        " v = m.encode(['hello world','refund flow','webhook'], "
        "normalize_embeddings=True);"
        f" mb = SentenceTransformer('{base_model}', trust_remote_code=True);"
        " vb = mb.encode(['hello world','refund flow','webhook'], "
        "normalize_embeddings=True);"
        " cos = float((v[0]*vb[0]).sum());"
        " assert cos < 0.999, "
        "f'FT did NOT change weights (cos={cos})';"
        " print('FT delta OK, cos=', cos)"
        '"'
    )

    train_smoke = (
        "python3 scripts/runpod/train_docs_embedder.py "
        f"--base={base_model} --train={train_data_remote} "
        "--steps=100 --i-know-im-using-tiny-data "
        f"--out={smoke_dir}"
    )

    # Every script lives under REMOTE_REPO_DIR after the overlay (Phase 0).
    # `cd` once at the start so all subsequent invocations see scripts/+src/.
    return " && ".join(
        [
            "set -euxo pipefail",
            f"cd {REMOTE_REPO_DIR}",
            py_capture_base,
            train_smoke,
            py_verify_delta,
            bench_step,
        ]
    )


def _make_train_command(
    *,
    kind: str,
    base_model: str,
    train_data_remote: str,
    out_dir_remote: str,
    max_steps: int,
    extra_args: str = "",
) -> str:
    """Bash command for the full-train step (run on pod).

    Routes to the right trainer based on `kind`:
      * "docs"     -> ``scripts/runpod/train_docs_embedder.py`` (bi-encoder ST)
      * "reranker" -> ``scripts/runpod/train_reranker_ce.py`` (cross-encoder)

    The two trainers share enough of a CLI surface (``--base``/``--train``/
    ``--steps``/``--out``) that the only thing that differs is the script
    path. Keeping the routing here means ``run_pipeline`` doesn't need to
    know about the per-kind trainer implementation details.
    """
    if kind == "reranker":
        trainer = "scripts/runpod/train_reranker_ce.py"
    else:
        trainer = "scripts/runpod/train_docs_embedder.py"
    parts = [
        "set -euxo pipefail",
        f"cd {REMOTE_REPO_DIR}",
        f"python3 {trainer} "
        f"--base={base_model} --train={train_data_remote} "
        f"--steps={max_steps} --out={out_dir_remote} {extra_args}".strip(),
    ]
    return " && ".join(parts)


def _make_bench_command(
    *,
    kind: str,
    candidate_tag: str,
    eval_data_remote: str,
    model_dir_remote: str,
    bench_json_remote: str,
) -> str:
    """Bash command for the full-bench step (writes JSON on pod)."""
    if kind == "reranker":
        return (
            "set -euxo pipefail && "
            f"cd {REMOTE_REPO_DIR} && "
            "python3 scripts/benchmark_doc_intent.py "
            f"--eval={eval_data_remote} --model=docs --rerank-on "
            f"--rerank-model-path={model_dir_remote} "
            f"--out={bench_json_remote}"
        )
    return (
        "set -euxo pipefail && "
        f"cd {REMOTE_REPO_DIR} && "
        f"CODE_RAG_HOME={REMOTE_REPO_DIR} python3 scripts/build_docs_vectors.py "
        f"--model={candidate_tag} --force && "
        "python3 scripts/benchmark_doc_intent.py "
        f"--eval={eval_data_remote} --model={candidate_tag} "
        f"--rerank-on --out={bench_json_remote}"
    )


def _make_hf_push_command(
    *,
    model_dir_remote: str,
    hf_repo: str,
) -> str:
    """Bash one-liner that pushes the FT artifact to HF Hub.

    The pod's HF_TOKEN env var (set at spawn time) is consumed by
    `huggingface_hub.HfApi.upload_folder`. We push to a private repo.
    """
    return (
        "set -euxo pipefail && "
        'python3 -c "'
        "from huggingface_hub import HfApi;"
        " api = HfApi();"
        f" api.create_repo('{hf_repo}', private=True, exist_ok=True);"
        f" api.upload_folder(folder_path='{model_dir_remote}',"
        f" repo_id='{hf_repo}')"
        '"'
    )


# ----- Poll loop guarantee ---------------------------------------------------


def _poll_until_bench_json_exists(
    bench_json_path: Path,
    *,
    interval_sec: int = POLL_INTERVAL_SEC,
    max_wait_sec: int = POLL_MAX_WAIT_SEC,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> bool:
    """Block until `bench_json_path` exists OR max_wait_sec elapses.

    Returns True if the file landed within the budget, False on timeout. The
    caller maps False → EXIT_ORPHAN_DETECTED (the pod may still be running
    somewhere; manual sweep needed).

    `sleep_fn` and `now_fn` are injection points for tests so the unit suite
    can simulate "file lands on the 3rd poll" without burning real time.
    """
    deadline = now_fn() + max_wait_sec
    while True:
        if bench_json_path.exists():
            return True
        if now_fn() >= deadline:
            return False
        sleep_fn(interval_sec)


# ----- Top-level orchestrator ------------------------------------------------


def run_pipeline(
    *,
    candidate_tag: str,
    kind: str,
    base_model: str,
    train_data: Path,
    eval_data: Path,
    gpu: str,
    max_steps: int,
    hf_repo: str,
    bench_json_path: Path | None = None,
    ssh_key_path: Path = DEFAULT_SSH_KEY,
    ssh_pub_path: Path = DEFAULT_SSH_PUB,
    volume_gb: int = 20,
    time_limit_min: int = SMOKE_TIME_LIMIT_MIN,
    spending_cap_usd: float = 5.0,
    poll_interval_sec: int = POLL_INTERVAL_SEC,
    poll_max_wait_sec: int = POLL_MAX_WAIT_SEC,
    spawn_fn: Callable[..., dict] | None = None,
    ssh_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    scp_back_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    scp_to_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    tar_overlay_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    repo_root: Path | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> PipelineResult:
    """Run the full 7-step pipeline for a single candidate.

    Contract (Bug 5):
      - tries to bring the pipeline to one of the 4 explicit terminal states
        (success, smoke-failed, train-failed, bench-failed) before returning;
      - GUARANTEES pod stop in a try/finally before returning;
      - GUARANTEES the function does NOT return until either bench JSON exists
        on Mac OR a definite failure code is set.

    All `*_fn` parameters default to the real subprocess/runpod implementations
    but tests inject mocks so the suite stays offline.
    """
    if kind not in ("docs", "reranker"):
        raise ValueError(f"--kind must be 'docs' or 'reranker', got {kind!r}")

    spawn_fn = spawn_fn or _spawn_pod
    ssh_fn = ssh_fn or _ssh_run
    scp_back_fn = scp_back_fn or _scp_back
    scp_to_fn = scp_to_fn or _scp_to
    tar_overlay_fn = tar_overlay_fn or _tar_overlay_to
    repo_root = repo_root or REPO_ROOT
    bench_json_path = bench_json_path or (DEFAULT_BENCH_DIR / f"{candidate_tag}_bench.json")

    result = PipelineResult(
        exit_code=EXIT_ORPHAN_DETECTED,  # pessimistic default
        candidate_tag=candidate_tag,
        bench_json_path=bench_json_path,
    )

    # --- Step 1: spawn pod -------------------------------------------------
    pod: dict | None = None
    try:
        try:
            pod = spawn_fn(
                candidate_tag=candidate_tag,
                gpu=gpu,
                volume_gb=volume_gb,
                time_limit_min=time_limit_min,
                ssh_public_key_path=ssh_pub_path,
                spending_cap_usd=spending_cap_usd,
            )
        except (PodLifecycleError, Exception) as e:
            _log(result, f"FAIL spawn: {e}")
            result.failure_step = "spawn"
            result.failure_reason = str(e)
            # No pod → no orphan possible. Map to smoke-failed for symmetry
            # (no train, no bench, no pod stop needed).
            result.exit_code = EXIT_SMOKE_FAILED
            return result

        result.pod_id = pod.get("pod_id") or pod.get("id")
        ssh_host = pod.get("ssh_host")
        ssh_port = pod.get("ssh_port")
        if not (result.pod_id and ssh_host and ssh_port):
            _log(result, f"FAIL spawn: pod missing ssh fields: {pod!r}")
            result.failure_step = "spawn"
            result.failure_reason = "pod missing ssh_host/ssh_port"
            result.exit_code = EXIT_SMOKE_FAILED
            return result
        _log(result, f"pod {result.pod_id} up at {ssh_host}:{ssh_port}")

        # --- Step 1.5: provision pod (setup_env + Phase-0 overlay) ---------
        # Without this the pod has only the stock pytorch image — no
        # scripts/, no src/. setup_env.sh installs deps + clones origin/main,
        # then we tar-overlay our local working tree on top so the pod sees
        # Phase-0 fixes (Bugs 1-5 + new eval builders) that aren't on GitHub
        # yet. Failure here maps to EXIT_SMOKE_FAILED with failure_step
        # "provision" — pod is stopped via the outer finally. No train is
        # attempted because the pod can't run our scripts.
        provision_ok = _provision_pod(
            host=ssh_host,
            port=ssh_port,
            key_path=ssh_key_path,
            repo_root=repo_root,
            result=result,
            ssh_fn=ssh_fn,
            scp_to_fn=scp_to_fn,
            tar_overlay_fn=tar_overlay_fn,
        )
        if not provision_ok:
            result.exit_code = EXIT_SMOKE_FAILED
            return result

        # --- Step 2a: scp eval+train data to pod ---------------------------
        for src in (train_data, eval_data):
            cp = scp_to_fn(
                host=ssh_host,
                port=ssh_port,
                local_path=Path(src),
                remote_path=f"/workspace/{Path(src).name}",
                key_path=ssh_key_path,
            )
            if cp.returncode != 0:
                _log(result, f"FAIL scp_to {src}: rc={cp.returncode}")
                result.failure_step = "scp_in"
                result.failure_reason = (cp.stderr or "")[:300]
                result.exit_code = EXIT_SMOKE_FAILED
                return result

        train_data_remote = f"/workspace/{train_data.name}"
        eval_data_remote = f"/workspace/{eval_data.name}"

        # --- Step 2b: 7-step smoke ----------------------------------------
        smoke_cmd = _make_smoke_command(
            kind=kind,
            candidate_tag=candidate_tag,
            base_model=base_model,
            train_data_remote=train_data_remote,
            eval_data_remote=eval_data_remote,
        )
        cp = ssh_fn(host=ssh_host, port=ssh_port, cmd=smoke_cmd, key_path=ssh_key_path)
        if cp.returncode != 0:
            _log(result, f"FAIL smoke: rc={cp.returncode} stderr={(cp.stderr or '')[:300]}")
            result.failure_step = "smoke"
            result.failure_reason = (cp.stderr or "")[:300]
            result.exit_code = EXIT_SMOKE_FAILED
            return result
        result.smoke_passed = True
        _log(result, "smoke passed")

        # --- Step 3: full train -------------------------------------------
        train_out_remote = f"/workspace/{candidate_tag}_full"
        train_cmd = _make_train_command(
            kind=kind,
            base_model=base_model,
            train_data_remote=train_data_remote,
            out_dir_remote=train_out_remote,
            max_steps=max_steps,
        )
        cp = ssh_fn(
            host=ssh_host, port=ssh_port, cmd=train_cmd, key_path=ssh_key_path, timeout_sec=4 * 3600
        )  # train can take hours
        if cp.returncode != 0:
            _log(result, f"FAIL train: rc={cp.returncode}")
            result.failure_step = "train"
            result.failure_reason = (cp.stderr or "")[:300]
            # Best-effort: scp partial logs back so post-mortem is possible.
            with contextlib.suppress(Exception):
                scp_back_fn(
                    host=ssh_host,
                    port=ssh_port,
                    remote_path=f"{train_out_remote}/training.log",
                    local_path=DEFAULT_BENCH_DIR / f"{candidate_tag}_train.log",
                    key_path=ssh_key_path,
                )
            result.exit_code = EXIT_TRAIN_FAILED
            return result
        _log(result, "train OK")

        # --- Step 5 (early): HF push so model survives pod stop -----------
        # Done BEFORE bench so a bench failure still preserves the artifact.
        hf_cmd = _make_hf_push_command(
            model_dir_remote=train_out_remote,
            hf_repo=hf_repo,
        )
        cp = ssh_fn(host=ssh_host, port=ssh_port, cmd=hf_cmd, key_path=ssh_key_path, timeout_sec=30 * 60)
        if cp.returncode == 0:
            result.train_pushed_to_hf = True
            _log(result, f"train pushed to HF: {hf_repo}")
        else:
            _log(result, f"WARN HF push failed rc={cp.returncode}; continuing to bench anyway")

        # --- Step 4: full bench --------------------------------------------
        bench_json_remote = f"/tmp/{candidate_tag}_bench.json"
        bench_cmd = _make_bench_command(
            kind=kind,
            candidate_tag=candidate_tag,
            eval_data_remote=eval_data_remote,
            model_dir_remote=train_out_remote,
            bench_json_remote=bench_json_remote,
        )
        cp = ssh_fn(host=ssh_host, port=ssh_port, cmd=bench_cmd, key_path=ssh_key_path, timeout_sec=2 * 3600)
        if cp.returncode != 0:
            _log(result, f"FAIL bench: rc={cp.returncode}")
            result.failure_step = "bench"
            result.failure_reason = (cp.stderr or "")[:300]
            result.exit_code = EXIT_BENCH_FAILED
            return result

        # --- scp bench JSON back to Mac (sentinel for poll loop) ----------
        cp = scp_back_fn(
            host=ssh_host,
            port=ssh_port,
            remote_path=bench_json_remote,
            local_path=bench_json_path,
            key_path=ssh_key_path,
        )
        if cp.returncode != 0:
            _log(result, f"FAIL scp_back bench JSON: rc={cp.returncode}")
            result.failure_step = "scp_back"
            result.failure_reason = (cp.stderr or "")[:300]
            result.exit_code = EXIT_BENCH_FAILED
            return result
        _log(result, f"bench JSON scp'd to {bench_json_path}")

        result.exit_code = EXIT_OK
        return result

    finally:
        # GUARANTEED pod stop, even on exception. Bug 5's whole point.
        _stop_pod_safely(result.pod_id, result)

        # Verify the stop took. If not, we leak a pod → orphan exit code.
        if result.pod_id and not _verify_pod_stopped(result.pod_id, result):
            # Don't downgrade an existing failure code (e.g. SMOKE) to ORPHAN
            # — keep the more-specific failure but log the orphan separately.
            if result.exit_code == EXIT_OK:
                result.exit_code = EXIT_ORPHAN_DETECTED
            _log(result, "WARN pod-orphan-detected (still RUNNING after stop)")

        # Step 7: poll-loop guarantee. The function MUST NOT return until the
        # bench JSON sentinel exists OR we set an explicit non-OK code. If
        # exit_code == OK but bench JSON isn't there yet (race between scp
        # rc=0 and filesystem flush, or future async-bench refactor), poll.
        if result.exit_code == EXIT_OK:
            ok = _poll_until_bench_json_exists(
                bench_json_path,
                interval_sec=poll_interval_sec,
                max_wait_sec=poll_max_wait_sec,
                sleep_fn=sleep_fn,
                now_fn=now_fn,
            )
            if not ok:
                _log(result, "FAIL poll: bench JSON did NOT appear within budget")
                result.failure_step = "poll"
                result.failure_reason = "bench JSON sentinel never appeared"
                result.exit_code = EXIT_ORPHAN_DETECTED


# ----- CLI -------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--candidate-tag", required=True, help="e.g. docs-nomic-ft-v2 (used for pod name + bench JSON path)")
    p.add_argument(
        "--kind", required=True, choices=("docs", "reranker"), help="Selects smoke variant + bench command shape"
    )
    p.add_argument("--base-model", required=True, help="HF id or local path to the base model")
    p.add_argument("--train-data", required=True, type=Path, help="JSONL training pairs/triplets")
    p.add_argument(
        "--eval-data", required=True, type=Path, help="JSONL eval set (canonical doc_intent_eval_v3_n200_v2)"
    )
    p.add_argument(
        "--gpu",
        required=True,
        choices=("4090", "a40", "a100"),
        help="GPU preset (alias-resolved to pod_lifecycle's GPU_PRESETS)",
    )
    p.add_argument("--max-steps", type=int, default=0, help="0 = full training pass (epochs-driven). >0 = capped.")
    p.add_argument("--hf-repo", required=True, help="Where to push FT artifact, e.g. Tarshevskiy/<tag>")
    p.add_argument("--volume-gb", type=int, default=20, help="Persistent volume size on /workspace (Bug 3 minimum: 20)")
    p.add_argument("--time-limit-min", type=int, default=SMOKE_TIME_LIMIT_MIN)
    p.add_argument("--spending-cap-usd", type=float, default=5.0)
    p.add_argument(
        "--bench-json", type=Path, default=None, help="Override default /tmp/<candidate>_bench.json sentinel path"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = run_pipeline(
        candidate_tag=args.candidate_tag,
        kind=args.kind,
        base_model=args.base_model,
        train_data=args.train_data,
        eval_data=args.eval_data,
        gpu=args.gpu,
        max_steps=args.max_steps,
        hf_repo=args.hf_repo,
        bench_json_path=args.bench_json,
        volume_gb=args.volume_gb,
        time_limit_min=args.time_limit_min,
        spending_cap_usd=args.spending_cap_usd,
    )
    # Emit a final summary JSON to stdout for the caller's parser.
    summary = {
        "candidate_tag": result.candidate_tag,
        "exit_code": result.exit_code,
        "pod_id": result.pod_id,
        "pod_stopped": result.pod_stopped,
        "smoke_passed": result.smoke_passed,
        "train_pushed_to_hf": result.train_pushed_to_hf,
        "bench_json_path": (str(result.bench_json_path) if result.bench_json_path else None),
        "failure_step": result.failure_step,
        "failure_reason": result.failure_reason,
    }
    print(json.dumps(summary, indent=2))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
