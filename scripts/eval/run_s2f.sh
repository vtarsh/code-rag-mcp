#!/usr/bin/env bash
# Steps-to-find bench, batched. Fresh python per BATCH_SIZE tasks.
# steps-to-find runs ≤5 hybrid_search calls per task (vs diagnose_recall.py's 1-2),
# so memory grows faster — BATCH_SIZE defaults to 10 (vs diagnose's 50) to keep
# the resident set bounded and protect 16GB Macs.
#
# Usage:
#   BATCH_SIZE=10 COUNT=10 OUT_DIR=bench_runs/improve/s2f_test ./scripts/eval/run_s2f.sh
#   # With arms:
#   CODE_RAG_NO_RERANK=1 OUT_DIR=bench_runs/improve/s2f_norerank ./scripts/eval/run_s2f.sh
#
# Env arms (carried into each python subprocess):
#   CODE_RAG_NO_RERANK=1   reranker stubbed (raw RRF order)
#   CODE_RAG_NO_VECTOR=1   vector leg stubbed (FTS-only)

set -u

CODE_RAG_HOME="${CODE_RAG_HOME:-$HOME/.code-rag-mcp}"
export CODE_RAG_HOME
export ACTIVE_PROFILE=pay-com

# Production-intended search config (matches run_diagnose.sh).
export CODE_RAG_DEFAULT_EXCLUDE="package_usage,provider_doc,dictionary"
export CODE_RAG_RRF_K=40
export CODE_RAG_KEYWORD_WEIGHT=2.0
export CODE_RAG_DISABLE_DOCS_TOWER=1
export CODE_RAG_CODE_RERANKER=Tarshevskiy/pay-com-rerank-l12-ft-run1
export CODE_RAG_IDLE_UNLOAD_SEC=0
export CODE_RAG_FRONTEND_BOOST=1.3
export CODE_RAG_FRONTEND_DEMOTE=0.9
export CODE_RAG_BACKEND_BOOST=1.05
export CODE_RAG_USE_EXPAND_QUERY=1

EVAL_FILE="$CODE_RAG_HOME/profiles/pay-com/eval/jira_eval_clean_v2.jsonl"
BATCH_SIZE="${BATCH_SIZE:-10}"
POOL_LIMIT="${POOL_LIMIT:-200}"
N_STEPS="${N_STEPS:-5}"
K_READ="${K_READ:-3}"
COUNT="${COUNT:-}"   # empty = all
OFFSET="${OFFSET:-0}"
OUT_DIR="${OUT_DIR:-$CODE_RAG_HOME/bench_runs/improve/s2f}"

TOTAL_AVAILABLE=$(wc -l < "$EVAL_FILE" | tr -d ' ')
if [ -z "$COUNT" ]; then
  COUNT=$((TOTAL_AVAILABLE - OFFSET))
fi
TOTAL=$((COUNT))
mkdir -p "$OUT_DIR"

echo "=== steps-to-find: $TOTAL queries (offset=$OFFSET), batch=$BATCH_SIZE, n_steps=$N_STEPS, k_read=$K_READ ==="
echo "    out=$OUT_DIR"
echo "    NO_RERANK=${CODE_RAG_NO_RERANK:-0}  NO_VECTOR=${CODE_RAG_NO_VECTOR:-0}"
echo ""

batch_files=()
for off in $(seq "$OFFSET" "$BATCH_SIZE" $((OFFSET + TOTAL - 1))); do
  remaining=$((OFFSET + TOTAL - off))
  count=$((remaining < BATCH_SIZE ? remaining : BATCH_SIZE))
  out="$OUT_DIR/batch_$(printf '%04d' "$off").json"
  echo "--- offset=$off count=$count -> $out ---"
  python3 "$CODE_RAG_HOME/scripts/eval/bench_steps_to_find.py" \
    --eval="$EVAL_FILE" --out="$out" --offset="$off" --count="$count" \
    --n-steps="$N_STEPS" --k-read="$K_READ" --pool-limit="$POOL_LIMIT" \
    || echo "BATCH offset=$off FAILED"
  batch_files+=("$out")
done

# Merge
MERGE_OUT="$OUT_DIR/full_s2f.json"
python3 - "$MERGE_OUT" "${batch_files[@]}" <<'PY'
import json, sys
out = sys.argv[1]
pq = []
for path in sys.argv[2:]:
    try:
        pq.extend(json.load(open(path))["eval_per_query"])
    except Exception as e:
        print(f"skip {path}: {e}")

ok = [q for q in pq if "error" not in q]
n = len(ok) or 1
hits = [r for r in ok if r["steps_to_first_hit"] is not None]
full = [r for r in ok if r["steps_to_full_recall"] is not None]
max_steps = max((len(r["queries_used"]) for r in ok), default=5)
hit_rate_at_step = {
    str(K): round(sum(1 for r in hits if r["steps_to_first_hit"] <= K) / n, 4)
    for K in range(1, max_steps + 1)
}
agg = {
    "n": len(ok), "n_error": len(pq) - len(ok),
    "n_hit": len(hits),
    "n_full_recall": len(full),
    "mean_steps_to_first_hit": round(sum(r["steps_to_first_hit"] for r in hits) / max(1, len(hits)), 4) if hits else None,
    "mean_steps_to_full_recall": round(sum(r["steps_to_full_recall"] for r in full) / max(1, len(full)), 4) if full else None,
    "mean_terminal_recall": round(sum(r["terminal_recall"] for r in ok) / n, 4),
    "full_recall_rate": round(len(full) / n, 4),
    "hit_rate_at_step": hit_rate_at_step,
}
json.dump({"aggregates": agg, "eval_per_query": pq}, open(out, "w"), indent=2)
print("=== FULL STEPS-TO-FIND ===")
print(json.dumps(agg, indent=2))
print("Wrote", out)
PY

echo "=== run_s2f.sh DONE ==="
