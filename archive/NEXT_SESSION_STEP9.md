# Next session brief — handoff after Steps 6-8 (2026-05-25)

## Якщо ти нова сесія — прочитай це першим

Це продовження `recall-query-processing-fixes` branch. Сесія 2026-05-22..25
зробила багато роботи. Усі деталі в `ARCHITECTURE_STATUS.md` (секції Step 6,
Step 7, Step 8). Цей файл — швидкий quick-start.

## Що зашиплено (KEEP, default OFF env-gated)

### Search-quality fixes

- **task_hint param** на `search_tool()` — caller передає `"frontend"`/`"backend"`/`"backoffice"`,
  отримує 2.0x repo boost. Default `None` = байт-identical до baseline.
  `bench_runs/improve/agent_task_prefix/`: +4 hits / 8-task panel.

- **`CODE_RAG_AUTO_TASK_HINT=1`** — regex-extract JIRA prefix (BO-/PI-/HS-) з query
  string + map → task_hint. UX convenience для callers що включають task ID у query.

- **`CODE_RAG_DEMOTE_TEST_PATHS=1`** — POST-rerank 0.5x demote на
  `/tests/`, `/__tests__/`, `.spec.`, `.test.` коли query не має test-keywords.
  Q3 onboarding-flow query: 3 test files #2,3,5 → #6,7,8.

- **`CODE_RAG_DEMOTE_TOOLING_REPOS=1`** — POST-rerank 0.2x demote на
  `github-*-action`, `*-eslint-config`, `lint-*`, `config-*` (НЕ boilerplate-*).
  Q1 provider-integration: github-run-e2e-action rank 3 → 8.

- **`CODE_RAG_HARD_FILTER=1`** — коли провайдер у query, drop pool до
  provider-relevant repos PRE-rerank. PI-56 Nuvei real-world test:
  pool 320→69, rank 0 → 1 (top-1 = handle-activities.js).

- **`CODE_RAG_SCOPE_WARNING=1`** — prepend warning коли pool spans 5+ repos
  для short query (vague intent).

### Bench/prod parity fix

- `scripts/eval/diagnose_recall.py:89` — `CODE_RAG_QUERY_V2` default `"0"` → `"1"`
  щоб match prod. Раніше 23/665 bench fails були bench-only артефактами.

### Cron rebuild fix

- `scripts/full_update.sh` — 10+ path/timeout fixes після scripts/ refactor.
- 6 moved Python scripts — sys.path `.parent.parent.parent` замість `.parent.parent`.
- `scripts/build_vectors.py` — skip dot-prefix repos (`.github`).
- Cron був silently broken since ~May 5 — index DB May 16, vector index Apr 24.
- Post-rebuild smoke trigger (`tests/smoke_search.py` runs after every rebuild).

### Smoke suite

`tests/smoke_search.py` — 12 tests, 60s wall, regression baseline. Включає
PI-56 hard-filter regression test (real-world JIRA→PR mapping).

## Що FALSIFIED (don't try again)

- **Step 3 v1 / v2** (per-token RRF) — both NEGATIVE on pod n=665 (-7/-4 hits)
- **STRIP_META_TAGS** — NEUTRAL on pod n=665
- **FE_DEFAULT_BOOST** — REJECTED 2026-05-21
- **Step 4 camelCase indexing** — FALSIFIED premise (tokenizer already preserves)
- **5 reranker FT iterations** — all REJECTED (R1, mxbai-v1, v12, v12a, v6.2)
- **Narrow keyword removal from _BACKEND_KEYWORDS** — NET NEGATIVE
- **Pre-rerank score multipliers** — bounce off reranker. Use POST-rerank instead.

## Recommended production env

Скопіюй у `~/Library/Application Support/Claude/claude_desktop_config.json`
для work account MCP, або у `~/.claude-work/.claude.json` env:

```json
"env": {
  "CODE_RAG_HOME": "/Users/vaceslavtarsevskij/.code-rag-mcp",
  "ACTIVE_PROFILE": "pay-com",
  "CODE_RAG_QUERY_V2": "1",
  "CODE_RAG_USE_EXPAND_QUERY": "1",
  "CODE_RAG_USE_CAMELCASE_EXPAND": "1",
  "CODE_RAG_DEFAULT_EXCLUDE": "package_usage,provider_doc,dictionary",
  "CODE_RAG_DEMOTE_TEST_PATHS": "1",
  "CODE_RAG_DEMOTE_TOOLING_REPOS": "1",
  "CODE_RAG_HARD_FILTER": "1",
  "CODE_RAG_SCOPE_WARNING": "1",
  "CODE_RAG_AUTO_TASK_HINT": "1"
}
```

## Що ще варто зробити (priority order)

### 1. Push code to remote via MCP (USER GO needed)

Локальні зміни на гілці `recall-query-processing-fixes`, не пушнуто.
Push через `mcp__github__push_files` (gh CLI deny-listed per CLAUDE.md).

Файли що змінились:
- src/search/service.py (task_hint + auto + scope_warning + hard_filter wiring)
- src/search/hybrid.py (Step 3 v2 dead code env-OFF + demote + repo_allow_list)
- scripts/eval/diagnose_recall.py (QUERY_V2 parity — gitignored, won't push)
- scripts/full_update.sh (path fixes)
- scripts/build/build_index.py + build_graph.py + build_docs_vectors.py + build_shadow_types.py (sys.path)
- scripts/data/embed_missing_vectors.py (sys.path)
- scripts/build_vectors.py (dot-prefix filter)
- scripts/bench/benchmark_queries.py + benchmark_realworld.py (sys.path)
- scripts/analysis/detect_blind_spots.py (sys.path)
- tests/smoke_search.py (NEW file)
- ARCHITECTURE_STATUS.md (Steps 6-8 added)
- README.md (Recommended env section)
- tests/AGENTS.md (smoke entry)
- NEXT_SESSION_STEP9.md (this file)

### 2. Pod n=665 full validation (RunPod budget needed)

Не зроблено per "RunPod budget = 0" — деферриться. When budget returns:
- Paired bench QUERY_V2=0 vs =1 на n=665 (magnitude of bench/prod parity fix)
- HARD_FILTER vs baseline n=665 (does PI-56 win generalize?)
- DEMOTE_TEST_PATHS + DEMOTE_TOOLING_REPOS net delta n=665

### 3. Reranker swap research

Agent C (today) researched stock alt rerankers:
- `jinaai/jina-reranker-v2-base-multilingual` — CC-BY-NC license, code benchmark
- `mxbai-rerank-large-v2` — Apache 2.0, pod-only at 3.5GB

Memory: `project_runpod_stage_c_landed_2026_04_24` lists 5 prior FT
iterations — all REJECTED. ARCH_STATUS DO NOT list blocks new FT.

Path forward: swap to stock alt reranker (no FT) — needs pod budget + license decision.

### 4. ast-grep MCP tool

~2.5 days dev. Adds structural code search. Not blocked but multi-session.

### 5. Stale-index warning in MCP output

When query targets a repo with <5 chunks OR index timestamp >24h old → warn.
Quick win (~30 min impl), needs design discussion.

## Hard constraints (HARD per CLAUDE.md)

1. **NO retrieval-pipeline change kept без full n=665 run на pod GPU.**
   See `feedback_blind_smoke_insufficient`.
2. **NO reindex** without explicit user GO. See `feedback_no_auto_rebuild`.
3. **NO external LLM APIs** (`feedback_no_external_llm_apis`).
4. **NO gh CLI** — use `mcp__github__*`.
5. **NO fine-tune reranker/embeddings** (ARCH_STATUS DO NOT list).
6. **NO local n=665 bench on Mac** — LanceDB mmap → 12GB RSS per process,
   16GB Mac OOMs. See `feedback_no_local_n665_bench`.
7. **EVERY pipeline change env-gated, default OFF** until pod-validated.
8. **POST-rerank > pre-rerank** for any pool-composition tweak.
9. **Trace-first** between runs з різною policy — examine per-task trace,
   не лише агрегати. See `feedback_check_trace_between_runs`.

## Memory entries (read for context)

- `project_task_hint_landed_2026_05_22` — task_hint param impl + A/B
- `project_noise_demote_landed_2026_05_22` — test/tooling demote impl + A/B
- `project_hard_filter_scope_warning_2026_05_23` — HARD_FILTER + SCOPE_WARNING
- `project_cron_broken_path_fix_2026_05_23` — cron broken since May 5, fixed
- `project_phase1_autonomous_2026_05_22_night` — smoke suite + noise audit
- `project_step3_v2_falsified_2026_05_22` — per-token RRF leg refuted
- `project_stripmeta_neutral_2026_05_22` — STRIP_META_TAGS neutral
- `project_queryv2_parity_bug_2026_05_22` — bench/prod parity
- `project_h5_fix_refuted_2026_05_22` — H5 architectural finding
- `project_global_audit_2026_05_22` — 6 hypotheses tested

## Net branch trajectory

| Metric | Branch start | After Step 8 |
|---|---|---|
| hit@10 single-shot | 60.5% | ~71-72% |
| recall@10 | 15.2% | 18.3% |
| recall@pool | 42% | 49% (+7pp) |
| Noise rate top-5 | ~10% | 1.6% (25-q audit) |
| PI-56 NUVEI top-10 | 0 | 5 (rank 1) with HARD_FILTER |
| PI-65 INPAY indexed | 1 file | 34 files (after cron fix) |
| Smoke suite | none | 12/12 PASS |
| Cron status | silently broken 3+ weeks | working end-to-end |
