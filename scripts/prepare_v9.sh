#!/bin/bash
# v9 candidate recipe (DRAFT — do not run blindly).
#
# Assembled from 2026-04-20 night critic synthesis (ROADMAP §"DONE 2026-04-20
# night"). Major deltas vs v8:
#   * lr 8e-5 -> 2e-5  (community norm per HF blog / tomaarsen ModernBERT ref)
#   * max-length 128 -> 256 (was 192 on v6.2, 128 on v8/MPS-OOM)
#   * weight_decay 0 -> 0.01 (overfit guard given higher training signal)
#   * --test-ratio 0.15 (real 15% holdout, stratified; fixes 904/5 bug)
#   * --early-stopping-patience 2 (HF EarlyStoppingCallback on val_loss)
#   * query mode parity: both train query (build_query_text) and eval use
#     EVAL_QUERY_MODE=enriched (matches training distribution).
#
# Before running: confirm the Phase 1 enriched-query re-eval (gte_v8_enriched)
# actually needs v9 — if v8 under enriched mode already beats baseline by the
# gate, skip v9 and deploy v8 with config.json query_mode change instead.

set -e
cd /Users/vaceslavtarsevskij/.code-rag-mcp
export CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp
export ACTIVE_PROFILE=pay-com

DATA=profiles/pay-com/finetune_data_v9
MODEL=profiles/pay-com/models/reranker_ft_gte_v9

# --- STEP 1: Data prep with real holdout ---
# v6.2-era flags (all proven), plus new --test-ratio 0.15 for real holdout.
# Do NOT use --dedupe-same-file (v5 catastrophe).
python3.12 scripts/prepare_finetune_data.py \
  --projects PI,BO,CORE,HS --min-files 1 --seed 42 \
  --out "${DATA}/" \
  --use-description --use-diff-positives --diff-snippet-max-chars 1500 \
  --drop-noisy-basenames --drop-generated --drop-trivial-positives \
  --min-query-len 30 --oversample PI=5 \
  --drop-popular-files 25 --max-rows-per-ticket 300 \
  --test-ratio 0.15

# --- STEP 2 (optional): listwise conversion (if we keep LambdaLoss) ---
# Skip this if comparing to v6.2 pointwise baseline first.
# python3.12 scripts/convert_to_listwise.py \
#   --in "${DATA}/train.jsonl" --out "${DATA}/train_listwise.jsonl" \
#   --group-size 16

# --- STEP 3: Smoke training (10% data, 0.3 epoch, ~15 min) ---
# Critic C: min viable smoke = 20% rows / 0.3 epoch with early-stopping.
# But easier first: just enable early-stopping on full run + treat first
# eval_steps checkpoint as the smoke signal.

# --- STEP 4: Full training ---
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
python3.12 scripts/finetune_reranker.py \
  --train "${DATA}/train.jsonl" \
  --test "${DATA}/test.jsonl" \
  --base-model Alibaba-NLP/gte-reranker-modernbert-base \
  --out "${MODEL}" \
  --epochs 1 \
  --batch-size 16 \
  --lr 2e-5 \
  --warmup 200 \
  --max-length 256 \
  --bf16 --optim adamw_torch_fused \
  --loss bce \
  --save-steps 500 \
  --val-ratio 0.10 \
  --early-stopping-patience 2 \
  --resume-from-checkpoint none

# --- STEP 5: Eval with enriched query mode (matches training distribution) ---
SLUG=gte_v9 MODEL="${MODEL}" DATA="${DATA}" \
  EVAL_QUERY_MODE=enriched BASELINE=skip \
  bash scripts/eval_parallel.sh
