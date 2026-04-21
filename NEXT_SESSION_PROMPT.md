# Prompt for next session (copy-paste below)

---

Продовжую `code-rag-mcp` після 2026-04-21 afternoon breakthrough. **Перед будь-якою дією прочитай повністю `ROADMAP.md`** — особливо першу секцію §"2026-04-21 afternoon: Conditional enriched FTS fallback" та старий §"🌙 2026-04-21 overnight" для контексту.

Також прочитай пам'ять: `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/MEMORY.md` та всі файли за посиланнями.

## Поточний стан (головне)

- **У проді: `reranker_ft_gte_v8`** (listwise LambdaLoss, 285MB bf16). Не змінюй.
- **Conditional enriched FTS fallback** (`--fts-fallback-enrich`) — ПІДТВЕРДЖЕНО ПОВНИМ EVAL. Baseline r@10=0.7112 (+5.85pp з fallback), v8 r@10=**0.7622** (+6.67pp з fallback). v8 перевага над baseline ЗБЕРЕЖЕНА: Δr@10=**+5.09pp**, ΔHit@5=**+7.92pp**, net=+100 (146 impr / 46 regr). PROMOTE на повному гейті.
- **Canonical baseline updated**: старі FT порівнювались vs 0.6527 r@10. Нові — vs 0.7112.
- **Daemon на `:8742`** — може бути unload'нутим через eval. Якщо так, рестартни: `CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3.12 daemon.py &disown`.
- **Task D (real-query eval)** — sampling готовий (`scripts/sample_real_queries.py`, 400 queries у `profiles/pay-com/real_queries/sampled.jsonl`). Labeling НЕ зроблений — блокує на Anthropic API access або manual LLM-as-judge.

## Головне питання сесії

**"Task D labeling та/або v12 FT з новим baseline як референс?"**

Canonical baseline оновлено: 0.7112 r@10 (замість 0.6527). Будь-який майбутній FT порівнюється на цьому рівні. Gate thresholds (Δr@10 ≥ +0.02, ΔHit@5 ≥ +0.02, net ≥ 20) залишаються релевантними — їх семантика є RELATIVE, не absolute.

## Що НЕ робити

- Не тренуй нову модель до того як побачиш числа full-eval fallback.
- Не свопай reranker у config.json — v8 все ще винний.
- Не push через `gh` — тільки `mcp__github__*`, owner=vtarsh.
- Якщо full-eval впав — НЕ пере-запускай з аналогічним SLUG, спочатку подивись `logs/eval_gte_v8_fallback.shard*.log` на причину.

## Перший крок

`gte_v8_fallback.json` готовий і в ROADMAP вже реальні числа. Далі варіанти:
1. **Task D labeling** — 400 queries у `profiles/pay-com/real_queries/sampled.jsonl`. Labeling via API або manual LLM-as-judge. ~$5 + 4h spot-check.
2. **v12 FT** — якщо плануєш train. Recipe у `Proven FT recipe` нижче. Canonical baseline = 0.7112.
3. **Runtime query expansion** — аналог fallback для real user queries (LLM rewrite / identifier extraction). Потребує Task D для вимірювання.
4. Інша ідея — запусти critic agents проти нового baseline якщо не очевидно куди йти.

## Правила роботи

- **Push через `mcp__github__push_files`** (owner=vtarsh, repo=code-rag-mcp). Локальна git branch розходиться з origin — не merge'и, просто push вперед.
- **Маленькими кроками**: написати → verify → commit → далі.
- **Tests must pass** перед push: `python3.12 -m pytest tests/ -q` (зараз 337).
- **Pre-commit pytest hook** flakey ЛИШЕ під час FT training (не під eval/inference). Якщо падає — перевір manually.
- **Перед тренуванням** — MANDATORY sample check (5 train/5 test rows, visual compare).
- **Real holdout ОБОВ'ЯЗКОВО** — `--test-ratio 0.15` для v12+.

## Що наступного (після підтвердження fallback)

1. **Task D labeling** (400 queries, ~$5 + 4h calibration). Blocker: API access. Без цього Jira-eval / runtime distribution mismatch нікуди не дінеться.
2. **v12 FT** з fallback enabled у eval gate. Чи потрібен ще один цикл — вирішувати за числами fallback full-eval.
3. **Runtime query expansion** (LLM rewrite or identifier extraction) — аналог fallback для real user queries. Потребує Task D для вимірювання.
4. ❌ v6.2+v8 ensemble — SKIP (Jaccard 0.918, oracle +0.55pp).
5. ⏸ Dense retrieval FT — DEFER (12-24h re-embed, розподіл той самий).

## Proven FT recipe (якщо знадобиться)

```bash
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
python3.12 scripts/finetune_reranker.py \
  --train profiles/pay-com/finetune_data_vN/train.jsonl \
  --test profiles/pay-com/finetune_data_vN/test.jsonl \
  --base-model Alibaba-NLP/gte-reranker-modernbert-base \
  --out profiles/pay-com/models/reranker_ft_gte_vN \
  --epochs 1 --batch-size 16 --lr 5e-5 --warmup 200 --max-length 256 \
  --bf16 --optim adamw_torch_fused --loss mse \
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

## Critical pitfalls (full list у ROADMAP)

1. Rerank FT ceiling раніше здавався +3-5pp; afternoon breakthrough показав що 8.5% tickets були недосяжні для FTS. Unlock'нули → +7.21pp Δr@10.
2. Real-holdout MANDATORY (`--test-ratio 0.15`).
3. `max_length=256 + batch=32` OOMs MPS. Use batch=16.
4. FTS5 `_FTS_PRECLEAN` must strip all non-word/space/.-/ punctuation.
5. Reranker path у config.json ABSOLUTE (daemon cwd ≠ repo).
6. Jira r@10 gains ≠ runtime gains. Завжди перевіряй `benchmark_queries.py` + `benchmark_realworld.py` окремо.
7. Blanket enriched mode LAME FTS candidates (−15pp на PI). Тільки CONDITIONAL fallback (`--fts-fallback-enrich`) безпечний.

## Початковий запит (копіпастуй у новий сеанс)

```
Прочитай ROADMAP.md + memory. Вчорашній breakthrough підтверджений: conditional enriched FTS fallback дає +5.09pp v8 Δr@10 (PROMOTE) на повному eval. Canonical baseline тепер 0.7112. Вибір наступного кроку: (1) Task D labeling — 400 queries готові у sampled.jsonl; (2) v12 FT з новим baseline; (3) runtime query expansion; (4) запусти critic agents. Працюй автономно з checkpoints.
```
