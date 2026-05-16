#!/bin/bash
# Overnight bench: EXP2 baseline vs enabled vs camelCase expansion
# Run this before leaving for the day.
set -e

CODE_RAG_HOME="${CODE_RAG_HOME:-$HOME/.code-rag-mcp}"
export CODE_RAG_HOME
export ACTIVE_PROFILE=pay-com
# Disable idle watchdog — long evals can exceed 30 min default
export CODE_RAG_IDLE_UNLOAD_SEC=0

echo "=== Overnight Bench ==="
echo "Start: $(date)"

wait_for_daemon() {
  local tries=0
  while ! curl -sf http://127.0.0.1:8742/health > /dev/null 2>&1; do
    tries=$((tries + 1))
    if [ $tries -gt 60 ]; then
      echo "ERROR: Daemon failed to start after 60s"
      exit 1
    fi
    sleep 1
  done
}

kill_daemon() {
  local pid=$1
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    sleep 3
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" || true
      sleep 1
    fi
  fi
}

# 1. Start daemon
python3 daemon.py > logs/daemon_overnight.log 2>&1 &
DAEMON_PID=$!
echo "Daemon PID=$DAEMON_PID"
wait_for_daemon
echo "Daemon ready"

# Warm up
curl -s -X POST http://127.0.0.1:8742/tool/search \
  -H "Content-Type: application/json" \
  -d '{"query":"trustly callback","limit":3}' > /dev/null
echo "Daemon warmed up"

# 2. Eval EXP2 enabled (default)
echo "[1/3] Eval EXP2 enabled..."
python3 scripts/eval/eval_jira_daemon.py \
  --out=bench_runs/overnight_exp2_enabled.json
cp bench_runs/overnight_exp2_enabled.json bench_runs/overnight_run1.json

# 3. Eval baseline (kill-switch)
echo "[2/3] Eval baseline (REPO_PREFILTER_BOOST=1.0)..."
kill_daemon $DAEMON_PID
CODE_RAG_REPO_PREFILTER_BOOST=1.0 python3 daemon.py > logs/daemon_baseline.log 2>&1 &
DAEMON_PID=$!
wait_for_daemon
curl -s -X POST http://127.0.0.1:8742/tool/search \
  -H "Content-Type: application/json" \
  -d '{"query":"trustly callback","limit":3}' > /dev/null
python3 scripts/eval/eval_jira_daemon.py \
  --out=bench_runs/overnight_baseline.json
cp bench_runs/overnight_baseline.json bench_runs/overnight_run2.json

# 4. Eval camelCase expansion
echo "[3/3] Eval camelCase expansion..."
kill_daemon $DAEMON_PID
CODE_RAG_USE_CAMELCASE_EXPAND=1 python3 daemon.py > logs/daemon_camel.log 2>&1 &
DAEMON_PID=$!
wait_for_daemon
curl -s -X POST http://127.0.0.1:8742/tool/search \
  -H "Content-Type: application/json" \
  -d '{"query":"update merchant","limit":3}' > /dev/null
python3 scripts/eval/eval_jira_daemon.py \
  --out=bench_runs/overnight_camel.json
cp bench_runs/overnight_camel.json bench_runs/overnight_run3.json

# 5. Compare
kill_daemon $DAEMON_PID
echo ""
echo "=== Results ==="
echo "EXP2 enabled:"
python3 -c "import json; d=json.load(open('bench_runs/overnight_exp2_enabled.json')); print(f\"  hit@10 = {d['aggregates']['hit_at_10']:.2%}\")"
echo "Baseline:"
python3 -c "import json; d=json.load(open('bench_runs/overnight_baseline.json')); print(f\"  hit@10 = {d['aggregates']['hit_at_10']:.2%}\")"
echo "CamelCase expansion:"
python3 -c "import json; d=json.load(open('bench_runs/overnight_camel.json')); print(f\"  hit@10 = {d['aggregates']['hit_at_10']:.2%}\")"

echo ""
echo "Bootstrap CI comparison:"
python3 scripts/eval/bootstrap_eval_ci.py \
  --baseline=bench_runs/overnight_baseline.json \
  --candidates bench_runs/overnight_exp2_enabled.json bench_runs/overnight_camel.json \
  --metric=hit_at_10

echo ""
echo "End: $(date)"
