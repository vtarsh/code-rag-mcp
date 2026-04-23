# Next-Session Prompt — 2026-04-23 late evening

Copy this into the first user message of a new Claude Code session.

---

## Що відбулось попередньою сесією (коротко)

Сесія 2026-04-22→23 landed **26+ commits** на `vtarsh/code-rag-mcp`:

**Інфраструктура:**
- Phase 1+2 MCP surface cleanup (3 dead tools видалено, `brief` mode для analyze_task/search, footgun fixes)
- Daemon P0 `/admin/unload` race + JSONL log concurrency
- V12 FT data infra (regen candidates, holdouts, IM-NDCG gate, leakage check, dual-judge merger, MiniLM labeler)
- Eval gate file-level GT v2 (§1-§8 closed — helpers, stratified net dispatcher, sanity gate, stratified sampler)
- Bench_v2 infra (200-query stratified sampler + gate + runner, labeled)
- Chunker fixes (MAX_CHUNK enforcement / dedup / orphan filter) — requires reindex
- Scraper link-rewrite fix — requires re-scrape
- Cross-provider fan-out tool + doc-intent widening in `hybrid.py`
- Docs reorganization (7 MOCs + 3 нові, 100% reciprocity with 52 files, `_index.md`, CLAUDE.md linkify)
- Repo cleanup: ~5 GB видалено (2 orphan моделі v4/v6.2, 4 GB; stale logs/caches/archives)

**V12 training:**
- v12a FT **REJECT** — Δr@10 = −0.020 train / −0.030 test vs v8.
- v8 залишається прод.
- 12 версій fine-tune'у (v1-v12a) single-tower архітектури — всі marginal або negative.

**Root cause ідентифіковано:**
Single-tower архітектура: CodeRankEmbed embed'ить code + docs в одну vector-space, але тренована тільки на коді. Docs отримують near-random embeddings → vector stage blind для doc queries → reranker не може fix'ити те, чого не витягнув retrieval.

**12 FT ітерацій = sunk-cost spiral.** Потрібен архітектурний pivot, а не 13-та спроба reranker tweaking.

---

## Пріоритет 1: Two-tower архітектура (код + документація окремо)

**Ціль:** doc-intent queries нарешті виграють в **vector stage**, не покладаючись на keyword FTS + reranker bailouts.

### Design

1. **Code index (існуючий, залишити):**
   - Модель: `nomic-ai/CodeRankEmbed`
   - Table: `db/vectors.lance.coderank/`
   - Обсяг: усі `file_type` крім docs

2. **Docs index (новий):**
   - Модель: `BAAI/bge-m3` (рекомендовано) АБО `nomic-embed-text-v1.5`
   - Table: `db/vectors.lance.docs/`
   - Обсяг: `file_type IN ('doc', 'docs', 'gotchas', 'reference', 'provider_doc', 'task', 'flow_annotation', 'dictionary')`
   - Приблизно 45k chunks × 768-dim = ~5-8 GB

3. **Query router (extend існуючого `_query_wants_docs()`):**
   - Code-only → query code index
   - Doc-only → query docs index
   - Mixed/ambiguous → query both, RRF merge
   - Reranker потім поліryє merged кандидатів

### Агентський recipe

Запустити 4 паралельні агенти з `isolation: "worktree"` + `model: "opus"`:

**Agent A — Docs indexer (3-4 год):**
```
Read existing `src/index/builders/docs_chunks.py` + `docs_indexer.py`.
Create new `src/index/builders/docs_vector_indexer.py` + extend pipeline
to write doc embeddings to `db/vectors.lance.docs/` table. Use
`sentence-transformers` with BAAI/bge-m3 model; batch size 16, MPS backend
if available else CPU. Persist embedding meta (model_name, dim, chunk_id).
Unit tests in `tests/test_docs_vector_indexer.py` covering: batch processing,
dedup by content_hash, error handling when model load fails.
md5 workflow mandatory.
```

**Agent B — Query router + hybrid search extension:**
```
Extend `src/search/hybrid.py::hybrid_search()` to support `docs_index=True`
param. When query is doc-intent per widened `_query_wants_docs()`, query
docs index and skip code index (or both for mixed intent). Merge via RRF
before reranking. Expose `docs_index: bool = False` (default) through MCP
search proxy. Tests: cover pure-code query → code index only, pure-doc →
docs index only, mixed → both, router edge cases.
```

**Agent C — Embedding provider dual-model:**
```
Extend `src/embedding_provider.py` to lazy-load TWO models: CodeRankEmbed
+ new docs embedding (bge-m3). Memory guard — warn if combined RSS >8 GB.
Add `/admin/health` exposure of both loaded models. Update `models.py`
config to include docs model key. Tests for lazy load + reset_providers.
```

**Agent D — Reindex script + migration docs:**
```
Write `scripts/build_docs_vectors.py` — standalone script that re-embeds
ALL doc chunks from db/knowledge.db into the new LanceDB table. Log
progress every 500 chunks. Checkpoint every 5000 chunks so crash doesn't
lose work. Update `scripts/full_update.sh` to invoke this after chunker.
Add `profiles/pay-com/docs/gotchas/two-tower-migration.md` with operator
runbook (expected runtime, RAM, disk, how to rollback).
```

**Sequential після:** E2E test — run `benchmark_queries.py` + `benchmark_realworld.py` + `churn_replay.py` на two-tower config. Compare vs v8 single-tower baseline.

### Validation targets

- Doc-intent queries у top-10 для 80%+ cases (v8 зараз ~40-50%)
- Code queries не регресують (>=v8 numbers)
- `benchmark_realworld` composite ≥ 0.843 (v8 baseline)
- No daemon RSS regression >3 GB (acceptable for two-model overhead)

---

## Пріоритет 2: Reindex після chunker fixes (overnight)

Сесія 04-23 landed `chunker fixes` (MAX_CHUNK + dedup + orphan filter) — але DB ще має старі chunks. Потрібен повний reindex:
```
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com make build
```
Тривалість: 1-2 год, пік RAM ~20 GB. **Залежний від Пріоритету 1** — краще зробити two-tower docs + single reindex, а не 2 reindexes.

---

## Пріоритет 3: Re-scrape 21 missing providers

Per 2026-04-23 coverage audit — 21 провайдер взагалі без scraped docs: `credentials, configurations, features, aptpay, paynt, sandbox, crb, ecentric, epx, flexfactor, hyp, mapping, nets, payfuture, paymend, payoutscom, payulatam, safecharge, sepa, storage, aptpay`.

Команда:
```
python3 profiles/pay-com/scripts/tavily-docs-crawler.py <DOCS_URL> <provider>
```

Scraper post-processor fixed → нові scrape emіт правильні links (no 99.6% broken residue).

Parallel агентів: запустити 4-5 scrape jobs одночасно по топ-priority (credentials — 341 code chunks, конфіги для всіх провайдерів).

---

## Пріоритет 4: Reindex provider docs (Plaid split)

`provider-plaid-docs_llms-full.txt.md` = 5.5 MB single chunk — unreachable. Post chunker fix + reindex буде auto-split. Але специфічно: перевірити що Plaid docs тепер chunked коректно по їхньому sitemap.

---

## Правила проекту (критично)

- **Push via MCP only** — `mcp__github__push_files` or `delete_file`, owner=`vtarsh`.
- **No external LLM APIs** — local only (CodeRankEmbed, bge-m3, CrossEncoders).
- **No commits without explicit ask** — чекай підтвердження перед push.
- **Agent hallucinations:** ~30% rate — md5 compare pre/post edit mandatory, grep for new symbols.
- **Push_files size cap:** ≤5 файлів per commit (size cap silently truncates).
- **V8 stays prod** до two-tower v13 доведе покращення.

---

## Memory файли критичні для next session

- `project_v12a_rejected_two_tower_pivot.md` — root cause аналіз + pivot rationale (НОВИЙ)
- `project_eval_gate_v2_landed.md` — eval gate v2 infrastructure (готова до v13)
- `project_v12_infra_landed.md` — дані інфра (холдаути, IM-NDCG) — reuse для v13
- `feedback_agent_hallucination_detection.md` — md5 workflow
- `feedback_push_files_size_cap.md` — ≤5 files/push
- `feedback_no_external_llm_apis.md` — local-only
- `feedback_code_rag_judge_bias.md` — judge patterns

---

## Перші кроки нової сесії

1. Прочитай `ROADMAP.md` остання секція + memory files вище.
2. Запусти 4 паралельні agents Пріоритету 1 (docs indexer, router, dual-model provider, reindex script).
3. Очікуй ~3-4 години, валідуй через pytest + benchmark.
4. Reindex (Пріоритет 2) overnight.
5. Next day — validate two-tower vs v8 + decide deploy.

**Success metric:** two-tower landed + benchmarks show 80%+ doc-intent queries у top-10 (vs ~40-50% на single-tower v8).
