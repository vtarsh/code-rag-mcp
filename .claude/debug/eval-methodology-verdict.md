# Eval methodology verdict — 2026-04-25 by team-lead

## Recommendation: **FIX-EVAL-FIRST**, no GO

3 незалежних критики, троє конвергують на одному verdict'і. Жодних розбіжностей. Запуск A/B на $5-10 без fix'ів = noise або false winner.

---

## Сонячна система блокерів

### P0 — broken benchmark, нічого не міряє (significance + metric)

**`scripts/benchmark_doc_intent.py:196,199` читає `row.get("gold")` + `row["expected_files"]`. Eval JSONL пише `gold=False` + `expected_paths`.** Результат: recall@10 = None для КОЖНОЇ моделі. Поточний "baseline" не існує. Будь-який порівняльний звіт = vacuous.

Fix: ~5 хв (rename keys у бенчмарку АБО у JSONL — обидва прийнятні; скоріше у бенчмарку).

### P0 — 45% expected_paths це "stock auto-docs" (eval-critic H2)

`data-layer/`, `codebase-map.md`, `architecture.md`, `README.md` тощо — висаджуються як expected_paths по 45% усіх (E,row) instances. **11/44 рядків (25%) мають 100% stock-paths** — обидві моделі знайдуть/не-знайдуть однаково, незалежно від якості docs-tower. Labeler — snapshot поточного FTS5 ranker, НЕ ground truth.

**Бомба:** нова модель яка знаходить better-but-different docs ПОКАРАНА false-negative.

Fix: drop 11 all-stock rows + relabel що залишились.

### P0 — n=44 не може розрізнити будь-який кандидат від шуму (significance)

| Test | MDE |
|---|---|
| Single arm Wilson 95% CI | ±13-14pp |
| Paired McNemar α=0.05 power=0.80 | **18pp** |
| 10-way Bonferroni (5 candidates → 10 pairs) | **25pp** |
| FWER без correction | **40%** |

Очікувані дельти: nomic-v2-moe +3-5pp, gte-large +5-10pp, fine-tune +1-15pp. **Жоден кандидат не дає ≥18pp надійно.** A/B → "no decision" або noise-driven cherry-pick.

Fix: expand eval до n≥100 (MDE 10pp) або n≥200 (MDE 5pp).

### P1 — train/eval path-leakage 27% (eval-critic H3)

12/44 eval rows містять ≥1 expected_path що з'являється у train positives для іншого запиту. Модель memorizes "цей path good", inflate'ує recall.

Fix: path-disjoint validator у `build_doc_intent_eval.py`.

### P1 — head distribution mass wrong (eval-critic H1)

Top-30 term coverage 28/30 (нормально). АЛЕ:
- payout: 193 prod vs **4 eval** (48× under-rep)
- provider: 117 vs **5** (23×)
- webhook: 97 vs **6** (16×)

Голова розподілу майже не тестується. Win на eval ≠ win на prod.

Fix: додати 10-15 prod-sampled rows для head terms.

### P1 — Hit@10 binary masquerades as Recall@10 (metric)

Поточна "Recall@10" — насправді Hit@10 (any expected in top-10 → 1). Втрачає інформацію коли |E|>1: модель що знаходить 1 з 5 expected = модель що знаходить 5 з 5. Bias до wide-retrieval.

Fix: справжній Recall@10 = |E∩R|/min(|E|,K).

### P2 — single metric → cherry-picking risk (metric M6)

Один номер дає cherry-pick attack (вибрати кращу моделю по тій метриці де вона виграла). Multi-metric AND-gate надійніше:

```
DEPLOY iff:
  Recall@10 ≥ baseline + 0.10
  AND nDCG@10 ≥ baseline + 0.05
  AND no per-stratum Δ < -0.15
  AND Hit@5 ≥ baseline - 0.05
  AND latency p95 < 2× baseline
```

### P3 — vector-only vs end-to-end (metric M7)

Поточний бенч міряє vector-only retrieval. Production має reranker_ft_gte_v8 поверх. Model swap може програти на vector-only але виграти end-to-end (чи навпаки). Треба додати end-to-end як sanity.

---

## Що треба зробити перед A/B (3 sequential phases, ~2-4h)

### Phase 1 — code fixes (~30 хв, parallel-safe)
- F1: schema fix у `benchmark_doc_intent.py` — 1 line
- F2: true Recall@10 implementation — ~10 lines
- F3: path-disjoint validator у `build_doc_intent_eval.py` — ~15 lines
- F4: 5-condition AND-gate scoreboard — ~30 lines

### Phase 2 — eval-v2 generation (~1-2h, may need user)
- Drop 11 all-stock rows
- Eliminate 16 train-leaked (row,path) instances
- Add 15 prod-sampled head-term rows (payout/gateway/validation/error)
- Multi-signal labeler v2 (BM25 + path-overlap + content-overlap + glossary-match consensus) — reduces stock-bias
- **Optional but recommended:** user spot-checks 30 random rows (~30 min) → confidence interval on labeler accuracy
- Output: `doc_intent_eval_v2.jsonl`, n=50-60 rows

### Phase 3 — sample size decision
- Якщо n=50-60 + ≥+10pp threshold = OK → запуск A/B
- Якщо потрібно ловити +5pp → expand до n=200 (~80 min user labeling, або 200-row auto-heuristic-v2 з v2 labeler)

---

## Оновлений план A/B після fix'ів

5 кандидатів:
1. Tarshevskiy/pay-com-docs-embed-v0 (вже існує, 10-pair fine-tune)
2. Tarshevskiy/pay-com-docs-embed-v1 (Stage D, 103-pair fine-tune, ~$0.14)
3. nomic-ai/nomic-embed-text-v2-moe (swap)
4. Alibaba-NLP/gte-large-en-v1.5 (swap)
5. baseline: nomic-ai/nomic-embed-text-v1.5 (control)

Optional 6: Snowflake/snowflake-arctic-embed-l-v2.0 / BAAI/bge-m3 (deferred per docs-research memory якщо top 5 inconclusive)

Cost: ~$0.14 (fine-tune) + 4 локальних build на Mac sequentially (~3h) АБО все на одному pod (~$2-3, ~1h).

Total project cost: ~$3 з $15 буфера.

---

## Bottom line для користувача

**FIX-EVAL-FIRST** — обов'язково. Запуск зараз = $5+ на noise.

Часові оцінки 3 шляхів:
- **Path A (мінімум):** Phase 1 (~30 хв agent) + run A/B з n=44 + threshold ≥+10pp. Half-broken eval, але хоча б benchmark не vacuous. Може дати "all candidates inconclusive" verdict.
- **Path B (рекомендовано):** Phase 1 + Phase 2 з 30-row user spot-check + n≈55 + threshold ≥+10pp. Чесний, але +5pp сигнал недосяжний.
- **Path C (gold standard):** Phase 1 + Phase 2 + n=200 (через auto-heuristic-v2 + 30-row spot-check OR full manual labeling). Може ловити +5pp realistic lifts. ~3-4h. Найшвидше через паралельний labeler agent.

User вирішує A/B/C. Не рекомендую запускати RunPod тести до того як Path A мінімум landed.
