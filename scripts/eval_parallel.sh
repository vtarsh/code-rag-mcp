#!/bin/bash
# Parallel 3-shard eval template (reusable). Runs all 3 shards concurrently.
# Expected speedup: ~40-50% vs sequential (MPS serializes kernels but CPU-side
# baseline reuse + data loading overlap well). Peak MPS memory: ~3GB.
#
# Usage:
#   SLUG=gte_v8 MODEL=profiles/pay-com/models/reranker_ft_gte_v8 \
#   DATA=profiles/pay-com/finetune_data_v8 bash /tmp/eval_parallel_template.sh

set -e
cd /Users/vaceslavtarsevskij/.code-rag-mcp
export CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp
export ACTIVE_PROFILE=pay-com

SLUG="${SLUG:?SLUG env required (e.g. gte_v8)}"
MODEL="${MODEL:?MODEL env required}"
DATA="${DATA:?DATA env required}"
BASELINE="${BASELINE:-profiles/pay-com/finetune_history/gte_v1.json}"
BASE_MODEL="${BASE_MODEL:-Alibaba-NLP/gte-reranker-modernbert-base}"
# Must match the baseline's eval_config for --reuse-baseline-from to pass
# the strict config check. gte_v1.json was produced with batch_size=2.
EVAL_BATCH="${EVAL_BATCH:-2}"
EVAL_MAXLEN="${EVAL_MAXLEN:-256}"

OUT=profiles/pay-com/finetune_history
mkdir -p logs

echo "=== EVAL_START slug=${SLUG} ts=$(date +%Y-%m-%dT%H:%M:%S) ==="
echo "MODEL=${MODEL}"
echo "DATA=${DATA}"

# Preflight: ensure daemon is down
pkill -9 -f "daemon.py" 2>/dev/null || true
sleep 2

# Launch 3 shards in parallel
PIDS=()
for i in 0 1 2; do
  (
    PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 \
    PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
    python3.12 scripts/eval_finetune.py \
      --base-model "${BASE_MODEL}" \
      --ft-model "${MODEL}" \
      --history-out "${OUT}/${SLUG}.json" \
      --shard-index ${i} --shard-total 3 \
      --reuse-baseline-from "${BASELINE}" \
      --manifest "${DATA}/manifest.json" \
      --training-summary "${MODEL}/training_summary.json" \
      --batch-size "${EVAL_BATCH}" --max-length "${EVAL_MAXLEN}" \
      2>&1 | tee "logs/eval_${SLUG}.shard${i}.log"
    echo "=== EVAL_SHARD_DONE slug=${SLUG} shard=${i} ts=$(date +%Y-%m-%dT%H:%M:%S) ==="
  ) &
  PIDS+=($!)
  # Stagger by 2s to avoid MPS init race
  sleep 2
done

# Wait for all shards
echo "Waiting for 3 shards (PIDs: ${PIDS[@]})..."
FAILED=0
for pid in "${PIDS[@]}"; do
  if ! wait "$pid"; then
    echo "SHARD_FAILED pid=$pid"
    FAILED=1
  fi
done

if [ $FAILED -ne 0 ]; then
  echo "=== EVAL_ABORTED slug=${SLUG} — at least one shard failed ==="
  exit 1
fi

# Merge
python3.12 scripts/merge_eval_shards.py \
  --out "${OUT}/${SLUG}.json" \
  --manifest "${DATA}/manifest.json" \
  --shards "${OUT}/${SLUG}.shard0of3.json" "${OUT}/${SLUG}.shard1of3.json" "${OUT}/${SLUG}.shard2of3.json" \
  2>&1 | tee "logs/eval_${SLUG}_merge.log"

echo "=== EVAL_DONE slug=${SLUG} ts=$(date +%Y-%m-%dT%H:%M:%S) ==="
