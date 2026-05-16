#!/usr/bin/env bash
# Pod bootstrap. Run after SSH'ing into a fresh RunPod pod.
#
# This script is the FIRST step for a fresh pod. It installs system packages,
# clones the public repo, and authenticates with HuggingFace.
#
# AFTER this, run setup_pod.sh (from this repo) to extract data archives
# and install project-specific Python deps.
#
# Workflow:
#   1. setup_env.sh       ← this file (fresh pod: system deps + clone)
#   2. setup_pod.sh       ← extract archives, fix compat, install deps
#   3. bench_large_models.py  ← run the bench
#
# For the full workflow, see: scripts/runpod/README.md
#
# HF auth: requires HF_TOKEN in env (or `huggingface-cli login --token <T>`).
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
REPO_URL="https://github.com/vtarsh/code-rag-mcp.git"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> [1/4] System packages"
apt-get update -qq
apt-get install -y -qq git python3-pip ca-certificates curl jq rsync

echo "==> [2/4] Python deps (sentence-transformers, lancedb, hf-hub, psutil)"
"$PYTHON_BIN" -m pip install --quiet --upgrade pip
"$PYTHON_BIN" -m pip install --quiet \
    "sentence-transformers>=3.0,<6.0" \
    "lancedb" \
    "huggingface-hub" \
    "psutil" \
    "einops" \
    "datasets" \
    "accelerate"

echo "==> [3/4] Clone code-rag-mcp (public)"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"
if [ -d "code-rag-mcp/.git" ]; then
    echo "    repo already cloned — pulling"
    (cd code-rag-mcp && git pull --ff-only)
else
    git clone --depth=1 "$REPO_URL"
fi

echo "==> [4/4] HF auth check"
# Pod env injected via start_pod is NOT inherited by SSH bash sessions, so
# ${HF_TOKEN} here is usually empty. The orchestrator (oneshot_*.py) writes
# the token to /workspace/.hf-token via a separate SSH command before this
# script runs; we read from there if env is empty.
HF_TOKEN_RESOLVED="${HF_TOKEN:-}"
if [ -z "$HF_TOKEN_RESOLVED" ] && [ -f /workspace/.hf-token ]; then
    HF_TOKEN_RESOLVED="$(cat /workspace/.hf-token)"
fi
if [ -n "$HF_TOKEN_RESOLVED" ]; then
    "$PYTHON_BIN" -c "from huggingface_hub import login; login(token='${HF_TOKEN_RESOLVED}', add_to_git_credential=False)"
    echo "    HF auth OK"
else
    echo "    WARN: HF_TOKEN not set in env or /workspace/.hf-token. Push will fail."
fi

echo ""
echo "========================================"
echo "Base env ready. Next: run setup_pod.sh"
echo "========================================"
echo ""
echo "If you have already uploaded data archives to the pod:"
echo "  cd /workspace/code-rag-mcp"
echo "  bash scripts/runpod/setup_pod.sh"
echo ""
echo "If you need to upload archives first, from your Mac run:"
echo "  rsync -avz --partial db/*.tar.gz profiles/pay-com/*.tar.gz runpod:/workspace/code-rag-mcp/"
echo ""
echo "For full docs: scripts/runpod/README.md"
echo "========================================"
