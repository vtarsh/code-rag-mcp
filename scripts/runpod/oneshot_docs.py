"""One-shot orchestrator for a single docs-tower (bi-encoder) candidate.

Mirror of ``oneshot_rerank.py`` but for the docs tower. The candidate's
model_key MUST already be registered in ``src.models.EMBEDDING_MODELS``
with ``.name = hf_repo`` so that ``build_docs_vectors --model={key}``
can resolve it after the HF push step uploads the FT'd weights.

Steps:
  1. spawn pod (rtx4090 / a40, purpose=train, volume_gb=20)
  2. provision (setup_env.sh + tar overlay scripts/src/lance + scp knowledge.db)
  3. scp train_data + eval_data
  4. train: train_docs_embedder.py
  5. HF push (so the registry's .name points to a real repo for build step)
  6. build_docs_vectors --model={tag} --force  (writes its own lance dir)
  7. bench: benchmark_doc_intent --model={tag} --rerank-on
  8. scp bench JSON back
  9. stop pod (try/finally)

Usage::

    source ~/.runpod/credentials
    python3 scripts/runpod/oneshot_docs.py \\
        --candidate-tag=docs-nomic-ft-run1 \\
        --base-model=nomic-ai/nomic-embed-text-v1.5 \\
        --train-data=/tmp/r1_cosent_triplets_v3.jsonl \\
        --eval-data=profiles/pay-com/doc_intent_eval_v3.jsonl \\
        --hf-repo=Tarshevskiy/pay-com-docs-nomic-ft-run1
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.runpod import pod_lifecycle  # noqa: E402

REMOTE_REPO_DIR = "/workspace/code-rag-mcp"
DEFAULT_SSH_KEY = Path("~/.runpod/ssh/RunPod-Key-Go").expanduser()
DEFAULT_SSH_PUB = Path("~/.runpod/ssh/RunPod-Key-Go.pub").expanduser()


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _ssh(host: str, port: int, cmd: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "ssh",
            "-i",
            str(DEFAULT_SSH_KEY),
            "-p",
            str(port),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=20",
            f"root@{host}",
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _scp_to(host: str, port: int, local: Path, remote: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "scp",
            "-i",
            str(DEFAULT_SSH_KEY),
            "-P",
            str(port),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            str(local),
            f"root@{host}:{remote}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _scp_back(host: str, port: int, remote: str, local: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "scp",
            "-i",
            str(DEFAULT_SSH_KEY),
            "-P",
            str(port),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"root@{host}:{remote}",
            str(local),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _tar_overlay(host: str, port: int, local_dirs: list[str], timeout: int = 1200) -> subprocess.CompletedProcess:
    for d in local_dirs:
        if not (REPO_ROOT / d).is_dir():
            return subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr=f"missing local dir: {d}")
    cmd = (
        "set -o pipefail; "
        f"COPYFILE_DISABLE=1 tar --no-xattrs -czf - -C {str(REPO_ROOT)!r} {' '.join(local_dirs)} | "
        f"ssh -i {str(DEFAULT_SSH_KEY)!r} -p {port} "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o LogLevel=ERROR root@{host} "
        f"'mkdir -p {REMOTE_REPO_DIR} && cd {REMOTE_REPO_DIR} && tar --no-same-owner -xzf -'"
    )
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate-tag", required=True, help="Must match a key in src.models.EMBEDDING_MODELS")
    p.add_argument("--base-model", required=True)
    p.add_argument("--train-data", required=True, type=Path)
    p.add_argument("--eval-data", required=True, type=Path)
    p.add_argument("--hf-repo", required=True)
    p.add_argument("--gpu", default="a40")
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=8, help="Lower (e.g. 4) for big bi-encoders like mxbai-large.")
    p.add_argument(
        "--max-seq-length",
        type=int,
        default=512,
        help="Cap encoder context. nomic/gte 8192 default OOMs on a40 at bs=16.",
    )
    p.add_argument("--loss", default="mnrl", choices=["mnrl", "cosent", "marginmse", "tsdae"])
    p.add_argument("--time-limit-min", type=int, default=120)
    p.add_argument("--spending-cap-usd", type=float, default=2.5)
    p.add_argument("--bench-json", type=Path)
    args = p.parse_args()

    bench_json = args.bench_json or Path(f"/tmp/{args.candidate_tag}_bench.json")
    bench_json.parent.mkdir(parents=True, exist_ok=True)

    if not args.train_data.is_file():
        sys.exit(f"--train-data missing: {args.train_data}")
    if not args.eval_data.is_file():
        sys.exit(f"--eval-data missing: {args.eval_data}")
    if not (REPO_ROOT / "db" / "knowledge.db").is_file():
        sys.exit(f"db/knowledge.db missing under {REPO_ROOT}")

    # Verify model_key is registered before spawning a pod (Bug 6b guard).
    sys.path.insert(0, str(REPO_ROOT))
    from src.models import EMBEDDING_MODELS

    if args.candidate_tag not in EMBEDDING_MODELS:
        sys.exit(
            f"--candidate-tag={args.candidate_tag!r} not in src.models.EMBEDDING_MODELS. "
            f"Register it with .name={args.hf_repo!r} before running."
        )

    pod_id = None
    try:
        if not DEFAULT_SSH_PUB.exists():
            sys.exit(f"SSH public key not found: {DEFAULT_SSH_PUB}")
        if not DEFAULT_SSH_KEY.exists():
            sys.exit(f"SSH private key not found: {DEFAULT_SSH_KEY}")
        ssh_pubkey = DEFAULT_SSH_PUB.read_text().strip()

        env = {}
        if hf_token := os.environ.get("HF_TOKEN"):
            env["HF_TOKEN"] = hf_token

        _log(
            f"spawning {args.gpu} pod for {args.candidate_tag} (cap=${args.spending_cap_usd}, time_limit={args.time_limit_min}m)"
        )
        pod = pod_lifecycle.start_pod(
            gpu=args.gpu,
            secure_cloud=True,
            time_limit_min=args.time_limit_min,
            spending_cap_usd=args.spending_cap_usd,
            volume_gb=20,
            ssh_public_key=ssh_pubkey,
            env=env or None,
            purpose="train",
            name=f"oneshot-{args.candidate_tag}",
        )
        pod_id = pod.get("id") or pod.get("podId") or pod.get("pod_id")
        if not pod_id:
            sys.exit(f"start_pod returned no id: {pod}")
        _log(f"pod {pod_id} created; waiting for SSH ready")

        ssh = pod_lifecycle.wait_for_ssh_ready(pod_id, timeout_sec=300)
        host = ssh["ssh_host"]
        port = ssh["ssh_port"]
        _log(f"SSH ready: {host}:{port}")

        # ---- provision ----
        _log("provision: scp setup_env.sh")
        cp = _scp_to(host, port, REPO_ROOT / "scripts" / "runpod" / "setup_env.sh", "/workspace/setup_env.sh")
        if cp.returncode != 0:
            sys.exit(f"FAIL scp setup_env.sh: rc={cp.returncode} {cp.stderr[:300]}")

        # Bug 6L: write HF token to /workspace/.hf-token before setup_env.sh
        # so it can be picked up regardless of SSH session env quirks.
        if hf_token := os.environ.get("HF_TOKEN"):
            cp = _ssh(
                host,
                port,
                f"umask 077 && printf '%s' '{hf_token}' > /workspace/.hf-token",
                timeout=30,
            )
            if cp.returncode != 0:
                sys.exit(f"FAIL write hf-token: rc={cp.returncode}")

        _log("provision: bash setup_env.sh (~3-5 min)")
        cp = _ssh(host, port, "bash /workspace/setup_env.sh", timeout=20 * 60)
        if cp.returncode != 0:
            sys.exit(f"FAIL setup_env.sh: rc={cp.returncode} stderr={cp.stderr[:500]}")
        _log("provision: setup_env OK")

        _log("provision: tar overlay scripts/+src/+db/vectors.lance.docs (~30-60s)")
        cp = _tar_overlay(host, port, ["scripts", "src", "db/vectors.lance.docs"])
        if cp.returncode != 0:
            sys.exit(f"FAIL tar overlay: rc={cp.returncode} stderr={cp.stderr[:500]}")
        _log("provision: overlay OK")

        _log("provision: scp db/knowledge.db (209MB)")
        cp = _ssh(host, port, f"mkdir -p {REMOTE_REPO_DIR}/db", timeout=30)
        if cp.returncode != 0:
            sys.exit(f"FAIL mkdir remote db: {cp.stderr[:300]}")
        cp = _scp_to(host, port, REPO_ROOT / "db" / "knowledge.db", f"{REMOTE_REPO_DIR}/db/knowledge.db", timeout=600)
        if cp.returncode != 0:
            sys.exit(f"FAIL scp knowledge.db: rc={cp.returncode} stderr={cp.stderr[:300]}")
        _log("provision: knowledge.db OK")

        # ---- data ----
        _log("data: scp train + eval JSONL")
        train_remote = f"/workspace/{args.train_data.name}"
        eval_remote = f"/workspace/{args.eval_data.name}"
        for src, dst in [(args.train_data, train_remote), (args.eval_data, eval_remote)]:
            cp = _scp_to(host, port, src, dst)
            if cp.returncode != 0:
                sys.exit(f"FAIL scp {src.name}: rc={cp.returncode} {cp.stderr[:300]}")
        _log("data: OK")

        # ---- train ----
        train_out = f"/workspace/{args.candidate_tag}_ft"
        train_cmd = (
            "set -euxo pipefail && "
            f"cd {REMOTE_REPO_DIR} && "
            f"python3 scripts/runpod/train_docs_embedder.py "
            f"--base={args.base_model} --train={train_remote} "
            f"--epochs={args.epochs} --loss={args.loss} --steps=0 "
            f"--batch-size={args.batch_size} "
            f"--max-seq-length={args.max_seq_length} "
            f"--out={train_out}"
        )
        _log("train: starting (~10-30 min)")
        t0 = time.time()
        cp = _ssh(host, port, train_cmd, timeout=2 * 3600)
        if cp.returncode != 0:
            _log(f"FAIL train: rc={cp.returncode}")
            print("--- STDOUT (last 500 chars) ---")
            print((cp.stdout or "")[-500:])
            print("--- STDERR (last 1000 chars) ---")
            print((cp.stderr or "")[-1000:])
            sys.exit(cp.returncode)
        _log(f"train: OK ({time.time() - t0:.0f}s)")

        # ---- HF push (BEFORE build_docs_vectors so models.py.name resolves) ----
        push_cmd = (
            "set -euxo pipefail && "
            'python3 -c "'
            "import os;"
            " from pathlib import Path;"
            " from huggingface_hub import HfApi;"
            " token = os.environ.get('HF_TOKEN');"
            " p1 = Path('/workspace/.hf-token');"
            " p2 = Path('/root/.cache/huggingface/token');"
            " token = token or (p1.read_text().strip() if p1.exists()"
            " else (p2.read_text().strip() if p2.exists() else None));"
            " assert token, 'no HF_TOKEN in env, /workspace/.hf-token, or /root/.cache/huggingface/token';"
            " api = HfApi(token=token);"
            f" api.create_repo('{args.hf_repo}', private=True, exist_ok=True);"
            f" api.upload_folder(folder_path='{train_out}',"
            f" repo_id='{args.hf_repo}', token=token)"
            '"'
        )
        _log(f"hf push: → {args.hf_repo}")
        cp = _ssh(host, port, push_cmd, timeout=20 * 60)
        if cp.returncode != 0:
            _log(f"FAIL hf push rc={cp.returncode}")
            print("--- HF push STDOUT (last 800) ---")
            print((cp.stdout or "")[-800:])
            print("--- HF push STDERR (last 1500) ---")
            print((cp.stderr or "")[-1500:])
            sys.exit(cp.returncode)
        _log("hf push: OK")

        # ---- build vectors with FT'd model ----
        build_cmd = (
            "set -euxo pipefail && "
            f"cd {REMOTE_REPO_DIR} && "
            f"CODE_RAG_HOME={REMOTE_REPO_DIR} python3 scripts/build_docs_vectors.py "
            f"--model={args.candidate_tag} --force"
        )
        _log("build vectors: starting (~10-25 min)")
        t0 = time.time()
        cp = _ssh(host, port, build_cmd, timeout=2 * 3600)
        if cp.returncode != 0:
            _log(f"FAIL build vectors rc={cp.returncode}")
            print((cp.stderr or "")[-1000:])
            sys.exit(cp.returncode)
        _log(f"build vectors: OK ({time.time() - t0:.0f}s)")

        # ---- bench ----
        bench_remote = f"/tmp/{args.candidate_tag}_bench.json"
        bench_cmd = (
            "set -euxo pipefail && "
            f"cd {REMOTE_REPO_DIR} && "
            f"CODE_RAG_HOME={REMOTE_REPO_DIR} python3 scripts/benchmark_doc_intent.py "
            f"--eval={eval_remote} --model={args.candidate_tag} --rerank-on "
            f"--out={bench_remote}"
        )
        _log("bench: starting (~5-15 min)")
        t0 = time.time()
        cp = _ssh(host, port, bench_cmd, timeout=2 * 3600)
        if cp.returncode != 0:
            _log(f"FAIL bench rc={cp.returncode}")
            print((cp.stderr or "")[-1000:])
            sys.exit(cp.returncode)
        _log(f"bench: OK ({time.time() - t0:.0f}s)")

        # ---- scp bench JSON back ----
        cp = _scp_back(host, port, bench_remote, bench_json)
        if cp.returncode != 0:
            sys.exit(f"FAIL scp_back bench: rc={cp.returncode} stderr={cp.stderr[:300]}")
        _log(f"bench JSON → {bench_json}")

        return 0

    finally:
        if pod_id:
            try:
                pod_lifecycle.stop_pod(pod_id)
                _log(f"pod {pod_id} stopped")
            except Exception as e:
                _log(f"WARN failed to stop pod {pod_id}: {e}")


if __name__ == "__main__":
    sys.exit(main())
