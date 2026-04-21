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
# Query composition mode for eval. 'summary' matches legacy runs (gte_v1..v8);
# 'enriched' uses build_query_text (matches training distribution).
# When switching to 'enriched', pass BASELINE=skip because old snapshots were
# produced with query_mode=summary and reuse-config-check will refuse to mix.
EVAL_QUERY_MODE="${EVAL_QUERY_MODE:-summary}"
# Conditional enriched fallback for summary-mode tickets with 0 FTS candidates.
# Set FTS_FALLBACK_ENRICH=1 to enable; also pass BASELINE=skip (old snapshots
# were produced with fallback=False). Rescues ~77 tickets (~8.5% of 909) whose
# short Jira titles don't match any FTS chunk. 2026-04-21 experiment.
FTS_FALLBACK_ENRICH="${FTS_FALLBACK_ENRICH:-0}"
# P0a: route eval through src.search.hybrid.hybrid_search (FTS + vector RRF +
# code_facts/env_vars wiring + content-type boosts). Aligns eval to production
# serving pool. Pass BASELINE=skip because old fts_only snapshots aren't
# comparable (retrieval_mode strict-check will refuse to mix). Vector search
# adds ~0.5s/ticket — expect total ~60-90min vs ~35-45min for fts_only.
USE_HYBRID_RETRIEVAL="${USE_HYBRID_RETRIEVAL:-0}"
# LPT shard balancing: path to a previous history_out JSON whose
# `per_task_baseline[*].latency_s` drives greedy longest-processing-time-first
# shard split. Eliminates heavy-tail bunching (eval_v8_hybrid first run had
# shard0 finish baseline while shard2 still on 145/175 BO). Omit for the first
# hybrid run; once gte_v8_hybrid.json exists, pass it on subsequent runs.
LATENCY_PROFILE="${LATENCY_PROFILE:-}"

OUT=profiles/pay-com/finetune_history
mkdir -p logs

echo "=== EVAL_START slug=${SLUG} ts=$(date +%Y-%m-%dT%H:%M:%S) ==="
echo "MODEL=${MODEL}"
echo "DATA=${DATA}"

# Preflight: ensure daemon is down
pkill -9 -f "daemon.py" 2>/dev/null || true
sleep 2

# Build reuse flag (empty when BASELINE=skip)
REUSE_FLAG=""
if [ "${BASELINE}" != "skip" ]; then
  REUSE_FLAG="--reuse-baseline-from ${BASELINE}"
fi

# Build fallback flag
FALLBACK_FLAG=""
if [ "${FTS_FALLBACK_ENRICH}" = "1" ] || [ "${FTS_FALLBACK_ENRICH}" = "true" ]; then
  FALLBACK_FLAG="--fts-fallback-enrich"
fi

# Build hybrid-retrieval flag (P0a)
HYBRID_FLAG=""
if [ "${USE_HYBRID_RETRIEVAL}" = "1" ] || [ "${USE_HYBRID_RETRIEVAL}" = "true" ]; then
  HYBRID_FLAG="--use-hybrid-retrieval"
fi

# Build latency-profile flag for LPT balancing
LATENCY_FLAG=""
if [ -n "${LATENCY_PROFILE}" ]; then
  LATENCY_FLAG="--latency-profile ${LATENCY_PROFILE}"
fi

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
      ${REUSE_FLAG} \
      --manifest "${DATA}/manifest.json" \
      --training-summary "${MODEL}/training_summary.json" \
      --batch-size "${EVAL_BATCH}" --max-length "${EVAL_MAXLEN}" \
      --eval-query-mode "${EVAL_QUERY_MODE}" \
      ${FALLBACK_FLAG} \
      ${HYBRID_FLAG} \
      ${LATENCY_FLAG} \
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
