# Prompt for next session (copy-paste below)

---

Продовжую code-rag-mcp після 2026-04-21 evening breakthrough + post-audit.
Прочитай `ROADMAP.md` — особливо першу секцію з новим next-lever ranking (P0a/b/c, P1a/b, P2).
Також прочитай пам'ять: `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/MEMORY.md`.

## Поточний стан

- У проді: **v8** (listwise LambdaLoss, 285MB bf16, +5.09pp над baseline з fallback). НЕ чіпай.
- Canonical baseline: **r@10 = 0.7112 / Hit@5 = 0.8339** (з `--fts-fallback-enrich`).
- v8 eval: **r@10 = 0.7622 / Hit@5 = 0.9131**. PROMOTE, net=+100, 146 impr / 46 regr.
- **P0 BUG:** `eval_finetune.py` ≠ `hybrid.py` retrieval (FTS-only vs FTS+vector RRF + boosts). Всі 11 FT ітерацій тюнились на неправильний candidate pool.
- **Free lunch:** `code_facts_fts` (1659 rows) + `env_vars` (4753 rows) побудовано — не читаються з `src/search/*.py` на query time.
- **Mini bug:** `src/search/suggestions.py:72` — `node_type` замість `type`. 629 zero-result queries. 5 хвилин фікс.

## Головне питання

**"Почати з P0a (eval/prod parity) чи з P0c (wire unused tables)?"**

- P0a розблоковує всі майбутні FT — без неї v12 → глухий кут (тюнить на неправильний пул).
- P0c — найдешевший recall win, не блокує нічого.
- P0b — просто фіксни (5 хвилин).

Рекомендація: **P0b → P0c → P0a → P1 → (може v12)**.

## Перший крок

1. Пофіксити `src/search/suggestions.py:72` (`node_type` → `type`) + `pytest`.
2. Прочитати `src/search/hybrid.py` + `scripts/eval_finetune.py` — зрозуміти точну розходженість candidate pool.
3. Вирішити шлях P0a: updated-eval (eval через hybrid.py) vs pure-hybrid-rerank (production без boost, тільки rerank top-200).

## Що НЕ робити

- v12 FT ДО P0a (буде тюнити на неправильний пул знов).
- Blanket enriched query mode (−15pp на PI, перевірено Phase 1).
- Push через `gh` — тільки `mcp__github__*`, owner=vtarsh.
- Train без `--test-ratio 0.15` (real holdout mandatory).
- `--dedupe-same-file` (v5 catastrophe −16.67pp).

## Що наступного (після P0)

1. **P1a — Null_rank rescue** (42 tickets, +2-3pp r@10 est). Glossary injection з `conventions.yaml` / `glossary.yaml`, або Haiku 3.5 LLM rewrite (~$0.0002/query).
2. **P1b — Top-K churn replay** на 2308 real queries. Baseline vs v8+fallback, порівняти top-10 ranks. 4h daemon, $0, runtime transfer signal.
3. **P2 — v12 FT** (ТІЛЬКИ після P0a). Agent B recipe: listwise + lr=5e-5 + freeze bottom 6 ModernBERT layers + dense-neg hybrid + fallback-enriched training + real-holdout gate.
4. ❌ v6.2+v8 ensemble — SKIP (Jaccard 0.918, oracle +0.55pp).
5. ❌ LLM-as-judge full labeling — churn replay дає 80% сигналу за $0.
6. ⏸ Dense embedding FT — DEFER (12-24h re-embed).

## Proven FT recipe (якщо v12 після P0a)

```bash
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
python3.12 scripts/finetune_reranker.py \
  --train profiles/pay-com/finetune_data_vN/train.jsonl \
  --test profiles/pay-com/finetune_data_vN/test.jsonl \
  --base-model Alibaba-NLP/gte-reranker-modernbert-base \
  --out profiles/pay-com/models/reranker_ft_gte_vN \
  --epochs 1 --batch-size 16 --lr 5e-5 --warmup 200 --max-length 256 \
  --bf16 --optim adamw_torch_fused --loss lambdaloss \
  --save-steps 500 --val-ratio 0.10 --early-stopping-patience 2 \
  --resume-from-checkpoint none
```

**Data prep (real holdout):**
```bash
python3.12 scripts/prepare_finetune_data.py \
  --projects PI,BO,CORE,HS --min-files 1 --seed 42 \
  --out profiles/pay-com/finetune_data_vN/ \
  --use-description --use-diff-positives --diff-snippet-max-chars 1500 \
  --drop-noisy-basenames --drop-generated --drop-trivial-positives \
  --min-query-len 30 --oversample PI=5 \
  --drop-popular-files 25 --max-rows-per-ticket 300 \
  --test-ratio 0.15
```

## Critical pitfalls (full list у ROADMAP §Critical pitfalls)

1. Rerank FT "ceiling" раніше здавався +3-5pp; fallback breakthrough показав 8.5% tickets недосяжні для FTS. Unlock → +7.21pp Δr@10.
2. Real-holdout MANDATORY (`--test-ratio 0.15`).
3. `max_length=256 + batch=32` OOMs MPS. Use batch=16.
4. FTS5 `_FTS_PRECLEAN` must strip all non-word/space/.-/ punctuation.
5. Reranker path у config.json ABSOLUTE (daemon cwd ≠ repo).
6. Jira r@10 gains ≠ runtime gains. Завжди перевіряй `benchmark_queries.py` + `benchmark_realworld.py` окремо.
7. **Eval ≠ prod** (new). `eval_finetune.py` FTS-only, `hybrid.py` FTS+vector+RRF+boosts. Fix P0a перед v12.

## Правила роботи

- **Push через `mcp__github__push_files`** (owner=vtarsh, repo=code-rag-mcp, FULL file content per commit — overwrites remote).
- **Маленькими кроками**: написати → verify → commit → далі.
- **Tests must pass** перед push: `python3.12 -m pytest tests/ -q` (337 expected).
- **Pre-commit pytest hook** flakey ЛИШЕ під час FT training (не під eval/inference).
- **Перед тренуванням** — MANDATORY sample check (5 train/5 test rows, visual compare).

## Початковий запит (копіпастуй у новий сеанс)

```
Прочитай ROADMAP.md + memory. Вчорашній breakthrough підтверджений: fallback +5.09pp v8 Δr@10 PROMOTE. Post-audit знайшов P0 BUG (eval ≠ prod), free lunch (unused tables), mini bug. Почни з P0b suggestions.py fix, потім P0c code_facts_fts wiring, потім P0a eval/prod parity. Тільки після цього v12 FT (Agent B recipe у ROADMAP). Працюй автономно з checkpoints.
```
