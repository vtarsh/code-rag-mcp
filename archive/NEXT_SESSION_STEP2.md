# Session brief — Plan B Step 2: refined JIRA body enrichment

## Контекст (де ми зараз)

Проєкт `code-rag-mcp` у `/Users/vaceslavtarsevskij/.code-rag-mcp` — MCP-сервер
що індексує 552 репо (81 087 чанків) пайплайном FTS5 + LanceDB-vector +
reranker. Реальний споживач — ітеративний Claude-агент через MCP
`search`/`analyze_task`/`trace_*`.

Гілка: `recall-query-processing-fixes` (pushed to `origin`).
Останній commit: `8b420c3` (Step 1 closing).

## ✅ Step 1 ЗАКРИТИЙ — що зроблено

7 commits, `825f5f3..8b420c3`. Деталі в `ARCHITECTURE_STATUS.md` (Step 1 sections).

| Що | Результат |
|---|---|
| Метрика `steps-to-find` | Створена, працює, дискримінує (`scripts/eval/bench_steps_to_find.py`) |
| n=665 baseline (pod) | hit_rate@step5 = **65.1%** (433/665) |
| n=665 rerank-OFF arm | hit_rate@step5 = **60.0%** (399/665), −5.1pp |
| Test-retest noise (pod) | **0pp** (bit-identical 0/665) |
| Strata pattern | BO helps (−10pp без rerank), CORE hurts (+1.7pp без), HS/PI neutral |
| Fix #1 "skip rerank on ident" | **FALSIFIED** (−10pp), reverted |

**Чесний висновок:** rerank earns 5.1pp irreducible value, iteration не replace'ить
його повністю (B-team з debate помилявся). Keep rerank ON globally.

## 🎯 Step 2 — refined JIRA body enrichment (3-5 робочих днів)

**Призначення:** з 43 zero-recall тасок ~14 мають opaque/symptom titles
(BO-1016 "Prod bug UNKNOWN", CORE-2173 "Refactoring API Logs Flow" — etc).
Title дає нуль code-signal'у. **JIRA body містить code-anchored hints** —
але прямий concat (наївне A3 з 2026-05-20) дав −18pp.

### План Step 2 (з brief'a, не змінено)

1. **Окремий retrieval pass на body** (не concat у один запит).
2. **Витягувати ТІЛЬКИ code-anchored** з body: identifiers regex (camelCase,
   snake_case, hyphenated), error strings (`Error: Code: X`), file paths
   (`*.tsx`, `*.proto`), стандартизовані терміни. **Викидати prose**.
3. **Merge** top-K title-pass + top-K body-pass через **RRF**, не append.
4. **Sanitize body** від `:`, `=`, `/`, `'`, JWT-токенів, паролів (PI-60
   body має credential leak!) перед FTS.
5. **Env-gated**, default OFF.
6. Tasks з body coverage: 635/665 (95%, перевірено).

### Keep criterion (СУМАРНО, обидва — без цього REVERT)

- **+Δ на steps-to-find@step5** (на pod GPU, не Mac CPU)
- **Non-regression на hit@10** на n=665 (≥ baseline 0.7143)
- Якщо хоча б одне fails → revert.

### Файли що торкатимуться

- `src/tools/task_context.py` (НОВИЙ, був видалений у A3 revert'і)
- `src/search/service.py` (query preprocessing)
- `src/search/hybrid.py` (новий entry для multi-pass)
- `scripts/eval/diagnose_recall.py` (env-flag для eval-side enrichment)
- `scripts/eval/bench_steps_to_find.py` (можливо — щоб бенч викликав task_context)

## Constraints (HARD — нічого з цього не порушувати)

1. **NO retrieval-pipeline change kept без full n=665 run на pod GPU.**
   Test-retest зараз ZERO noise — будь-який Δ real signal.
2. **NO reindex** без `feedback_no_auto_rebuild` дозволу (peak 20GB RAM kill 16GB Mac).
3. **NO external LLM APIs** (`feedback_no_external_llm_apis`).
4. **NO gh CLI** — використовуй `mcp__github__*`.
5. **Кожна change env-gated, default OFF.**
6. Smoke + trace + full-665 на pod. Регресія → REVERT.
7. **Heavy job 15+ min** — попередь користувача перш ніж launch.

## Pod setup quickref (з досвіду цієї сесії)

```bash
# 1. Launch pod
source ~/.runpod/credentials
python3 scripts/runpod/pod_lifecycle.py --start --gpu=rtx4090 \
    --secure-cloud --purpose=bench --time-limit=90m --spending-cap=2

# 2. Wait for SSH (~1-2 хв), grab IP+port from --status

# 3. Add to ~/.ssh/config як `runpod-bench-YYYY-MM-DD`

# 4. Bootstrap env
source ~/.runpod/credentials
ssh runpod-bench-YYYY-MM-DD "export HF_TOKEN='$HF_TOKEN'; bash -s" \
    < scripts/runpod/setup_env.sh

# 5. Switch to working branch
ssh runpod-bench-YYYY-MM-DD "cd /workspace/code-rag-mcp && \
    git fetch origin recall-query-processing-fixes:recall-query-processing-fixes && \
    git checkout recall-query-processing-fixes && \
    pip install -q 'transformers>=4.45,<5.0' mcp pydantic PyYAML"

# 6. Upload small archives (~370MB, 30s at good speed)
rsync -avz --partial \
    db/knowledge.db.tar.gz \
    profiles/pay-com/models.tar.gz \
    profiles/pay-com/eval.tar.gz \
    runpod-bench-YYYY-MM-DD:/workspace/code-rag-mcp/

# 7. Extract + compat fix
ssh runpod-bench-YYYY-MM-DD 'cd /workspace/code-rag-mcp && \
    mkdir -p db profiles/pay-com && \
    mv knowledge.db.tar.gz db/ && mv models.tar.gz eval.tar.gz profiles/pay-com/ && \
    tar xzf db/knowledge.db.tar.gz && \
    tar xzf profiles/pay-com/models.tar.gz && \
    tar xzf profiles/pay-com/eval.tar.gz && \
    sed -i "s/def tracked\[\*\*P, T\]/def tracked/; s/\[\*\*P, T\](fn:/(fn:/" src/cache.py && \
    sed -i "s/def require_db\[\*\*P, T\]/def require_db/; s/\[\*\*P, T\](func:/(func:/" src/container.py'

# 8. Scp eval v2 (НЕ в tar.gz — створено 2026-05-20)
scp -i ~/.runpod/ssh/RunPod-Key-Go -P PORT \
    profiles/pay-com/eval/jira_eval_clean_v2.jsonl \
    root@IP:/workspace/code-rag-mcp/profiles/pay-com/eval/

# 9. Stream-extract vector tar.gz (~23 GB, ~20 хв при 18 MB/s)
ssh runpod-bench-YYYY-MM-DD "cd /workspace/code-rag-mcp && tar xzf -" \
    < db/vectors.lance.coderank.tar.gz

# 10. Run bench (rerank ON baseline)
ssh runpod-bench-YYYY-MM-DD 'cd /workspace/code-rag-mcp && \
    CODE_RAG_HOME=/workspace/code-rag-mcp ACTIVE_PROFILE=pay-com \
    CODE_RAG_TRACE=1 CODE_RAG_TRACE_LOG=/path/trace.jsonl \
    CODE_RAG_DEFAULT_EXCLUDE="package_usage,provider_doc,dictionary" \
    CODE_RAG_RRF_K=40 CODE_RAG_KEYWORD_WEIGHT=2.0 \
    CODE_RAG_DISABLE_DOCS_TOWER=1 \
    CODE_RAG_CODE_RERANKER=Tarshevskiy/pay-com-rerank-l12-ft-run1 \
    CODE_RAG_FRONTEND_BOOST=1.3 CODE_RAG_FRONTEND_DEMOTE=0.9 \
    CODE_RAG_BACKEND_BOOST=1.05 CODE_RAG_USE_EXPAND_QUERY=1 \
    python3 -u scripts/eval/bench_steps_to_find.py \
      --out=/path/to/output.json --offset=0 --count=665 > /path/log 2>&1'

# 11. Download results + terminate pod
rsync -avz runpod-bench-YYYY-MM-DD:/workspace/code-rag-mcp/path/ local_path/
source ~/.runpod/credentials && python3 scripts/runpod/pod_lifecycle.py \
    --terminate POD_ID
```

**Час на full pod cycle:** ~10 хв setup + 22 хв vector upload + 22 хв bench
= ~55 хв wall. Cost ~$0.65 / повний цикл.

## Ключові уроки цієї сесії (memory references)

- `feedback_blind_smoke_insufficient` — small samples (n=30, n=50) можуть дати
  ПРОТИЛЕЖНІ signals від full 665. Fix #1 на n=30 показав −10pp; на n=50
  стратум-pattern (HS hurts) інвертований vs full 665 (HS neutral). Завжди
  валідувати на full 665 на target платформі (pod GPU).
- `feedback_check_trace_between_runs` — між runs з різною policy/config
  обов'язково examine traces per-task. Не лише aggregates. Replay в
  `scripts/eval/replay_miss.py`.
- `feedback_no_auto_rebuild` — НЕ запускати build_index/build_vectors без
  explicit "go". 20GB RAM peak vbиває 16GB Mac.

## Перші кроки в новій сесії

1. **Прочитати ARCHITECTURE_STATUS.md** (Step 1 sections — кoнкретно
   "n=665 baseline + rerank-OFF arm DONE" та "Step 1 closing").
2. **Прочитати** `bench_runs/improve/causal_trace_analysis.md` — конкретні
   miss-mechanism findings з 11 replay'ів.
3. **Прочитати цей файл** (`NEXT_SESSION_STEP2.md`).
4. **Спитати користувача** перш ніж launching anything heavy. Зокрема:
   - Чи готовий запускати pod (cost ~$0.30-0.65)?
   - Чи є новий design preference для body-enrichment policy?
5. **Імплементація:** почати з `src/tools/task_context.py` (новий файл,
   extract code-anchored з body). Не торкати search pipeline до stage 2.
6. **Smoke** — single-task test на BO-1016 чи інший zero-recall з body.
7. **n=20-30 local** — sanity (швидко, без pod).
8. **n=665 на pod** — keep-decision (gated на крок 4 OK).

## Очікувані результати Step 2

- На 43 zero-recall задачах **~14 мають code-anchored body** → потенційно
  закриваємо 8-12 з них (=+1.2-1.8pp на full 665)
- Решта не змінюються (titles не opaque OR body теж generic)
- Reranker patterns не торкаємо
- Loss budget: 0 (will revert)

**Цільовий лифт Step 2: +1-2pp hit_rate@step5 + zero hit@10 regression.**

## Файли під рукою (важливі для Step 2)

- `bench_runs/improve/steps_to_find_design.md` — Step 1 design
- `bench_runs/improve/causal_trace_analysis.md` — root-cause findings
- `bench_runs/improve/s2f_v2_n665_baseline/full_s2f.json` — baseline reference
- `profiles/pay-com/eval/jira_eval_clean_v2.jsonl` — 665 eval rows
- `db/knowledge.db` — chunks DB (locally on Mac)
- `db/vectors.lance.coderank/` — vector index (locally)
- `db/vectors.lance.coderank.tar.gz` — 23 GB compressed, ready for pod upload

## Що НЕ робити

- ❌ Не намагатися "fix #1 повторно" (rerank-skip-on-ident) — falsified
- ❌ Не пускати fix без n=665 на pod
- ❌ Не оптимізувати recall@pool — структурна стеля (`project_recall_pool_diagnosis`)
- ❌ Не залишати pod alive overnight ($0.69/год)
- ❌ Не fine-tune reranker або embeddings (locked в ARCHITECTURE_STATUS)
- ❌ Не concat body у query (A3 був −18pp)
