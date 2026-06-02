# Session brief — Plan B Step 6+: continue lifting recall

## Контекст (де ми зараз)

Проєкт `code-rag-mcp` у `/Users/vaceslavtarsevskij/.code-rag-mcp` — MCP-сервер,
що індексує 552 репо (81 087 чанків) пайплайном FTS5 + LanceDB-vector +
reranker. Гілка: `recall-query-processing-fixes`.

**Remote head:** `0ce7f905` (Step 5 + docs).
**Local head:** `2cca130` (Step 5 commit) → has 2 commits ahead of remote with
Step 2/3 code (hybrid.py default flip, service.py FE-default-boost env-gate,
bench Step 2 flip, test_fts.py update, SUMMARY.md). These can be MCP-pushed
in next session if needed — they're env-gated default OFF except Step 2 which
matches remote behavior anyway.

## ✅ Що вже зроблено (summary)

| Step | Метрика | Status |
|---|---|---|
| Earlier (FIX-A/D/F/G/H + FTS5 sanitize) | +10.9pp hit@10, +3.0pp recall@10 | LANDED `22a996b`, `3eebeda` |
| Step 1 (s2f metric) | metric infra | DONE |
| Step 2 (body enrichment) | +3.31pp s2f@step5, +22 hits | LANDED default ON `7e25763`/`35cbdab`, local default-flip `3963929` |
| Step 3 v1 (per-token append) | NO-OP | env-OFF kept |
| Step 3 v3 (FE-default-boost) | −2 hits | env-OFF kept |
| Step 4 (camelCase indexing) | FALSIFIED PREMISE | cancelled before reindex |
| **Step 5 (camelCase EXPAND)** | **+5 hits, +1.38pp recall@pool, +0.34pp recall@10** | LANDED default ON `2cca130` |

## Net recall trajectory across the branch

| Метрика | Branch start | Now | Δ |
|---|---|---|---|
| hit@10 single-shot | 60.5% | **~69-71%** | **+9-11pp** |
| recall@10 | 15.2% | **18.3%** | **+3.1pp** |
| **recall@pool** | 42% | **49%** | **+7pp** |
| s2f@step5 (iterating) | 65.1% | 68.4% | +3.31pp |

## Constraints (HARD — нічого з цього не порушувати)

1. **NO retrieval-pipeline change kept без full n=665 run на pod GPU.**
   `feedback_blind_smoke_insufficient`: n=30/50 sample може дати інверсний
   signal від full 665.
2. **NO reindex** без explicit user GO. `feedback_no_auto_rebuild`: peak
   20GB RAM kill 16GB Mac.
3. **NO external LLM APIs** (`feedback_no_external_llm_apis`).
4. **NO gh CLI** — use `mcp__github__*`. Push code via MCP push_files.
5. **NO fine-tune reranker/embeddings** (ARCH_STATUS DO NOT list).
6. **Heavy job 15+ min** — попередь користувача перш ніж launch (pod, reindex).
7. **EVERY pipeline change env-gated, default OFF** until pod-validated.
8. **Між runs з різною policy/config — examine trace per-task** (NOT only
   aggregates). `feedback_check_trace_between_runs`. Pipeline можна
   trace-увімкнути через `CODE_RAG_TRACE=1 CODE_RAG_TRACE_LOG=/path.jsonl`.
   Replay через `scripts/eval/replay_miss.py`. Між кожними змінами:
   - До run: snapshot stable baseline trace
   - Після run: diff aggregates AND check per-task queries_used, found_at,
     body_query, body_fts_count, per_token_added, vec_err, fts_count=0
   - Якщо метрика рухає в інший бік ніж очікувано → відкривай trace для
     2-3 flipped tasks ПЕРШ НІЖ робити висновки

## Pod setup quickref

```bash
# 1. Launch pod
source ~/.runpod/credentials
python3 scripts/runpod/pod_lifecycle.py --start --gpu=rtx4090 \
    --secure-cloud --purpose=bench --time-limit=90m --spending-cap=3

# 2. Wait ~30s for SSH ready, grab IP+port via:
curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" "https://rest.runpod.io/v1/pods/POD_ID" | \
    python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('publicIp'), (d.get('portMappings') or {}).get('22'))"

# 3. Add to ~/.ssh/config as runpod-bench-YYYY-MM-DD

# 4. Bootstrap env
source ~/.runpod/credentials
ssh runpod-bench-YYYY-MM-DD "export HF_TOKEN='$HF_TOKEN'; bash -s" \
    < scripts/runpod/setup_env.sh

# 5. Switch branch + deps (user-approved on prior session via `!ssh` prefix)
ssh runpod-bench-YYYY-MM-DD 'cd /workspace/code-rag-mcp && \
    git fetch origin recall-query-processing-fixes:recall-query-processing-fixes && \
    git checkout recall-query-processing-fixes && \
    pip install -q "transformers>=4.45,<5.0" mcp pydantic PyYAML && echo SETUP_OK'

# 6. Upload archives + tasks.db + jira_eval_clean_v2.jsonl
rsync -avz --partial \
    db/knowledge.db.tar.gz \
    db/tasks.db \
    profiles/pay-com/models.tar.gz \
    profiles/pay-com/eval.tar.gz \
    profiles/pay-com/eval/jira_eval_clean_v2.jsonl \
    runpod-bench-YYYY-MM-DD:/workspace/code-rag-mcp/

# 7. Extract + Python 3.11 compat fix (PEP 695 generics)
ssh runpod-bench-YYYY-MM-DD 'cd /workspace/code-rag-mcp && \
    mkdir -p db profiles/pay-com/eval && \
    mv knowledge.db.tar.gz db/ && mv tasks.db db/ && \
    mv jira_eval_clean_v2.jsonl profiles/pay-com/eval/ && \
    mv models.tar.gz eval.tar.gz profiles/pay-com/ && \
    tar xzf db/knowledge.db.tar.gz && \
    tar xzf profiles/pay-com/models.tar.gz && \
    tar xzf profiles/pay-com/eval.tar.gz && \
    sed -i "s/def tracked\[\*\*P, T\]/def tracked/; s/\[\*\*P, T\](fn:/(fn:/" src/cache.py && \
    sed -i "s/def require_db\[\*\*P, T\]/def require_db/; s/\[\*\*P, T\](func:/(func:/" src/container.py'

# 8. scp diagnose_recall.py (NOT in git, gitignored under scripts/)
rsync -avz scripts/eval/diagnose_recall.py runpod-bench-YYYY-MM-DD:/workspace/code-rag-mcp/scripts/eval/

# 9. Stream-extract vector tar.gz (~28GB extracted, ~15min)
ssh runpod-bench-YYYY-MM-DD 'cd /workspace/code-rag-mcp && tar xzf -' \
    < db/vectors.lance.coderank.tar.gz

# 10. Run bench (recall@10 single-shot OR s2f for iterating)
ssh runpod-bench-YYYY-MM-DD 'cd /workspace/code-rag-mcp && \
    CODE_RAG_HOME=/workspace/code-rag-mcp ACTIVE_PROFILE=pay-com \
    CODE_RAG_TRACE=1 CODE_RAG_TRACE_LOG=/workspace/bench_runs/trace.jsonl \
    CODE_RAG_DEFAULT_EXCLUDE="package_usage,provider_doc,dictionary" \
    CODE_RAG_RRF_K=40 CODE_RAG_KEYWORD_WEIGHT=2.0 \
    CODE_RAG_DISABLE_DOCS_TOWER=1 \
    CODE_RAG_CODE_RERANKER=Tarshevskiy/pay-com-rerank-l12-ft-run1 \
    CODE_RAG_FRONTEND_BOOST=1.3 CODE_RAG_FRONTEND_DEMOTE=0.9 \
    CODE_RAG_BACKEND_BOOST=1.05 CODE_RAG_USE_EXPAND_QUERY=1 \
    python3 -u scripts/eval/diagnose_recall.py \
      --out=/workspace/bench_runs/recall.json --offset=0 --count=665 --pool-limit=200'

# 11. Download results + terminate pod (WAIT until after rsync before --terminate)
rsync -avz runpod-bench-YYYY-MM-DD:/workspace/bench_runs/ bench_runs/improve/XXX/
source ~/.runpod/credentials && python3 scripts/runpod/pod_lifecycle.py --terminate POD_ID
```

## 🎯 Remaining recall levers (priority order)

### A. Step 3 v2 — per-token union as SEPARATE RRF leg (design ready, no reindex)

v1 (per-token append) was NO-OP because candidates landed at ranks ≥151 with
RRF score ≈0.01, dominated by every other leg. Per-agent research (in
`tasks/research_v2_*.md` if saved, else re-launch):

**v2 design:** Don't append to keyword_results. Add separate `pt:{rowid}` keys
in scores dict with own weight `PT_RRF_WEIGHT=0.5`. Per-token rank-0 then gets
RRF = `0.5/(40+0+1) = 0.0122` — comparable to vector rank-41. Co-hits across
FTS+vector+PT compound additively (matches the existing fts+vec merge pattern).

**Estimated lift:** 2-3 tasks (BO-1460, CORE-2412, CORE-2507 — verified
candidates per agent recon). Modest but pod-bench would confirm.

**Implementation sketch** (hybrid.py):
1. New constants near `_PER_TOKEN_UNION`:
   ```python
   _PT_RRF_WEIGHT = float(os.getenv("CODE_RAG_PT_RRF_WEIGHT", "0.5"))
   ```
2. Replace v1 wiring (lines ~503-518) with stash:
   ```python
   per_token_results = fts_search_per_token(...) if _PER_TOKEN_UNION else []
   ```
3. After existing FTS+vector RRF loop (~line 627), add new loop for `pt:` keys:
   ```python
   for rank, sr in enumerate(per_token_results):
       key = f"pt:{sr.rowid}"
       rrf_score = _PT_RRF_WEIGHT / (K + rank + 1)
       if key not in scores:
           scores[key] = {...}
       scores[key]["score"] += rrf_score
       scores[key]["sources"].append("per_token")
   ```
4. Extend same-rowid merge (lines 632-645) to fold `pt:` into `fts:`/`vec:`.

### B. ast-grep structural search tool — additive, no risk, ~2.5 days

Adds AST-aware code search as a new MCP tool. Doesn't change retrieval —
gives the agent a precise structural query path (`find_calls_of(X)`,
`find_definitions(SymbolName)`). Per ARCH_STATUS, 10s install, covers
TS/TSX/JS/Go. Doesn't directly bump recall metrics but improves agentic-loop
effectiveness on tasks where the agent knows the exact symbol.

### C. analyze_task routing improvements

Honest baseline foothold@5 = 0.34 (n=200 de-leaked). Plenty of room.
CORE-domain-template was rejected by data, but BO-template works at 93%.
Other strata (HS, PI) might benefit from finer classifier. Not strictly
recall@10 lift but improves agent's first-shot repo selection.

### D. Other env knobs not yet tested

- `CODE_RAG_RERANK_POOL_SIZE` — tested 200→400 = no change. Skip.
- `CODE_RAG_BM25_PATH_WEIGHT` — tested 2026-05-20, REJECTED.
- `CODE_RAG_PER_TOKEN_UNION` = 1 — v1 NO-OP, redesign per Step 3 v2 above.
- `CODE_RAG_FE_DEFAULT_BOOST` = 1 — FALSIFIED n=30. Don't enable.
- `CODE_RAG_STRIP_META_TAGS` = 1 — not tested on full 665. Worth a try.
- `CODE_RAG_COVERAGE_HINT` — display-layer, not recall.

## Перші кроки в новій сесії

1. **Read this brief + last commit's ARCHITECTURE_STATUS.md "Step 5" section**
   (already on remote 0ce7f905).
2. **Check memory** for recent project entries:
   `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/`
   - `project_step5_camelexpand_landed_2026_05_22.md`
   - `project_step3_attempts_falsified_2026_05_21.md`
   - `project_step2_landed_2026_05_21.md`
3. **Verify current state** by reading remote `src/search/fts.py` line ~334:
   `CODE_RAG_USE_CAMELCASE_EXPAND` default should be `"1"`.
4. **Decide direction**: ask user if they want to try Step 3 v2 (per-token
   as separate RRF leg) or pivot to something else (ast-grep tool, routing,
   stratum-gated rerank-skip for CORE per Step 1 v2 finding).
5. **Trace-first protocol** for any change:
   - Snapshot baseline trace before tweak (full 665 with `CODE_RAG_TRACE=1`)
   - Apply tweak, run paired bench
   - DIFF traces per-task BEFORE looking at aggregates
   - For 3-5 flipped tasks: check queries_used, found_at, body_query,
     per_token_added, vec_err, fts_count
   - Use `scripts/eval/replay_miss.py` for deep inspection
6. **Pod-bench gating**: full 665 на pod GPU обов'язково перш ніж keep.

## Очікувані результати Step 6 (if Step 3 v2)

- 2-3 task recovery (BO-1460, CORE-2412, CORE-2507) → +0.3-0.5pp recall@10
- Pod n=665 ~$0.50, ~55min wall
- Net recall trajectory could reach ~19% recall@10 if this lands

## Що НЕ робити

- ❌ Не reindex без explicit user GO (Step 4 falsified — tokenizer fine)
- ❌ Не fine-tune reranker/embeddings (ARCH_STATUS DO NOT)
- ❌ Не trust малі samples — n=665 на pod єдиний honest signal
- ❌ Не залишати pod alive overnight (~$0.69/hr × 12h = $8)
- ❌ Не ship'ити default-ON без pod validation
- ❌ Не пропускати trace check between policy changes
