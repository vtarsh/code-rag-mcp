#!/usr/bin/env bash
# Pod bootstrap. Run after SSH'ing into a fresh RunPod pod.
#
# Installs: Python deps for fine-tuning + bench.
# Clones: vtarsh/code-rag-mcp (PUBLIC repo only — no private profile data).
#
# HF auth: requires HF_TOKEN in env (or `huggingface-cli login --token <T>`).
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
REPO_URL="https://github.com/vtarsh/code-rag-mcp.git"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> [1/4] System packages"
apt-get update -qq
apt-get install -y -qq git python3-pip ca-certificates curl jq

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

echo "==> Done. Workspace: $WORKSPACE/code-rag-mcp"
