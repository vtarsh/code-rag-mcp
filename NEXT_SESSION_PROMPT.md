# Next-Session Prompt — 2026-04-24 morning

Copy this into the first user message of a new Claude Code session.

---

## Що відбулось попередньою сесією (коротко)

Сесія 2026-04-23→24 landed **two-tower v13** (16 commits) на `vtarsh/code-rag-mcp`:

**Two-tower architecture LANDED + benchmarks GREEN:**
- Vector retrieval splits by query intent: code → CodeRankEmbed (768d), docs → nomic-embed-text-v1.5 (768d, новий tower).
- 45 759 doc chunks embedded into `db/vectors.lance.docs/` (153 MB on disk, IVF-PQ 64×48). Build ~25 хв на MPS.
- 651/651 pytest green.
- Benchmarks (vs v8 baseline):
  - queries: 0.9333 → 0.933 (flat, +0)
  - realworld: 0.843 → 0.843 (flat, +0)
  - flows: — → 0.833 (новий метрик)
- Live smoke test: `provider response mapping reference` повертає `docs/references/provider-response-mapping.md` top-1 без FTS fallback.

**Reranker:** залишається `reranker_ft_gte_v8` (без змін). Two-tower змінив тільки vector leg, не rerank.

**Memory budget на 16 GB Mac:** code 230 MB + docs 550 MB + reranker 285 MB ≈ 1.1 GB resident. Безпечно.

**Pay-com housekeeping:** ~50 commits на `vtarsh/pay-knowledge-profile`:
- `.gitignore` розширено (відсічено 2.3 GB бінарників: `reranker_ft_gte_v12a/`, `models/`, `finetune_data_*/`, `churn_replay/`, тощо).
- 41 видалень session scratch + data/.
- 14 modified configs (ROADMAP, RECALL-TRACKER, conventions.yaml, provider_types/*, recipes/*, scripts/*).
- 23 deletions docs reorg cleanup (phase8-*, pi-60-*, обсолетні gotchas).
- 13/14 modified files pushed.
- Не запушено: `recipes/new_apm_provider.yaml` (1 файл) + 334 нових Nuvei docs pages.

---

## Пріоритет 1: Реіндекс з chunker fixes (overnight, узгодити з юзером)

**Стан:** `db/knowledge.db` має chunks побудовані ДО chunker fixes (MAX_CHUNK enforcement, dedup, orphan filter). Two-tower vectors побудовані з цього СТАРОГО knowledge.db.

**Що дасть reindex:**
- Свіжі chunks без orphan headings/duplicates (особливо в provider docs).
- Великі md (Plaid 5.5MB) тепер коректно chunked по sections.
- Re-build обох tower'ів на оновлених chunks.

**Команда:**
```bash
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com make build
```

**КРИТИЧНО:** peak ~20 GB RAM. На 16 GB Mac = жорсткий swap. Рекомендую запускати overnight, юзер має не використовувати ноут.

**Тривалість:** 1-2 год (clones → extracts → indexes → graph → vectors code → vectors docs → benchmarks).

---

## Пріоритет 2: Doc-intent recall harness (валідація two-tower lift)

Поточний benchmark suite (4 conceptual + 6 realworld + 2 flow) — здебільшого code-intent, тому показав flat metric для two-tower. Потрібен doc-intent harness.

**Setup:**
- 20-30 queries з `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` де target — reference docs.
- Метрика: top-10 recall на doc-intent тільки.
- Цільова: ≥80% (vs ~40-50% на v8 single-tower per NEXT_SESSION_PROMPT 04-23).

**Скрипт:** `scripts/benchmark_doc_intent.py` (новий). Прогнати vs `docs_index=False` (force code tower) + `docs_index=True` (force docs tower) + `docs_index=None` (auto-route) для A/B/C порівняння.

---

## Пріоритет 3: Re-scrape 21 missing providers + finish pay-com push

Per 04-23 audit: 21 провайдер без scraped docs (`credentials, configurations, features, aptpay, paynt, sandbox, crb, ecentric, epx, flexfactor, hyp, mapping, nets, payfuture, paymend, payoutscom, payulatam, safecharge, sepa, storage, aptpay`).

**Команда:**
```bash
python3 profiles/pay-com/scripts/tavily-docs-crawler.py <DOCS_URL> <provider>
```

Після scrape: incremental update docs vectors:
```bash
python3 scripts/build_docs_vectors.py --repos=<provider1>,<provider2>,...
```

**Pay-com pushes що залишилось:**
- 1 файл: `recipes/new_apm_provider.yaml` (single push)
- 334 Nuvei docs pages: ~67 batches @ 5 files/batch — ~$3-5 в API tokens, окрема сесія. Local commit `560f9df` на disk.

---

## Пріоритет 4: Test-credentials hygiene (опціонально)

Pay-com репо містить реальні sandbox credentials в `docs/references/test-credentials/*.md` (наприклад `nuvei-provider.md` → `nuveiMerchantSecretKey: ...`). Pre-existing у історії, не від цієї сесії. Sandbox-only, низький ризик, але якщо хочеш чистоту:
- Перенести в Bitwarden/1Password/SOPS + шаблонізувати на плейсхолдери.
- `git filter-repo` cleanup (одноразово, потім force-push private).

---

## Правила проекту (критично)

- **Push via MCP only** — `mcp__github__push_files` / `create_or_update_file` / `delete_file`, owner=`vtarsh`.
- **No external LLM APIs** — local only (CodeRankEmbed, nomic-embed-text-v1.5, MiniLM CrossEncoders).
- **NEVER run >1 Python compute process at once** — 16 GB Mac freezes on parallel ML jobs (lesson from 04-23 evening: 4 build_docs_vectors processes = 21 GB virt RAM = freeze).
- **Sequential builds:** `python scripts/build_docs_vectors.py` runs ~25 min on MPS. Resume from `db/docs_checkpoint.json` if interrupted.
- **Daemon restart:** `kill -9 $(lsof -ti:8742); sleep 2; CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown`
- **Agent hallucinations:** ~30% rate. md5 compare pre/post edit; grep new symbols. NEXT_SESSION_PROMPT history shows agent reports drift from real state.
- **Push_files size cap:** ≤5 файлів per push or ≤3 if any ≥500 lines (silent truncation risk).
- **V8 reranker stays prod.** Two-tower лише змінив vector leg.

---

## Memory файли критичні для next session

- `project_two_tower_v13_landed.md` — what's deployed + how to use (НОВИЙ)
- `project_v12a_rejected_two_tower_pivot.md` — root cause why we pivoted
- `feedback_agent_hallucination_detection.md` — md5 workflow
- `feedback_push_files_size_cap.md` — ≤5 files/push
- `feedback_no_external_llm_apis.md` — local-only
- `feedback_push_via_mcp_not_gh.md` — MCP push only

---

## Перші кроки нової сесії

1. Прочитай `ROADMAP.md` остання секція (2026-04-24) + `project_two_tower_v13_landed.md` memory.
2. Перевір `db/vectors.lance.docs/` exists + 45759 vectors (`python3 -c "import lancedb; print(lancedb.connect('db/vectors.lance.docs').open_table('chunks').count_rows())"`).
3. Якщо потрібен реіндекс (Пріоритет 1) — узгодь з юзером overnight slot.
4. Інакше — Пріоритет 2 (doc-intent harness) як швидкий валідаційний win.

**Success metric для наступної сесії:** doc-intent recall harness показує ≥80% top-10 (vs ~40-50% baseline на single-tower v8).
