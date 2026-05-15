# Metric-Critic — Recall@10 для docs-tower A/B (T2)

**Author:** metric-critic teammate  
**Date:** 2026-04-24  
**Verdict:** **HOLD** на поточну метрику. Recall@10 у тому вигляді, як він зараз реалізований у `scripts/benchmark_doc_intent.py`, **не вимірює нічого корисного** на існуючому eval set, тому що он cтавить `n_gold=0`. Окремо — навіть коли його полагодити, single Recall@10 binary metric надто грубий для multi-path eval rows.

---

## Sanity-fact #1 (BLOCKER): метрика не активна

**Файл:** `scripts/benchmark_doc_intent.py:196-224`

```python
if row.get("gold"):
    n_gold += 1
    expected = {(e["repo_name"], e["file_path"]) for e in (row["expected_files"] or [])}
    ...
    recall_at_10 = (n_gold_hit / n_gold) if n_gold else None
```

Усі 44 рядки eval set (`profiles/pay-com/doc_intent_eval_v1.jsonl`) мають `"gold": false` і поле `expected_paths`, а не `expected_files`. Перевірено:

```
Total rows: 44
  gold=True:  0
  gold=False: 44
expected_paths length distribution: {5: 44}
has expected_files? 0
has expected_paths? 44
```

**Наслідок:** запуск `python3 scripts/benchmark_doc_intent.py --only docs` **завжди** повертає `recall@10 = None`, `n_gold_hits = 0/0`. Поточний summary block нічого не порівнює — це diagnostic-only режим (top-K dump у `prod_results`).

**Це треба пофіксити ПЕРЕД будь-якою дискусією про вибір метрики.** Один з двох:
1. Перейменувати `expected_files` → `expected_paths` у benchmark та трактувати усі 44 prod-row як gold (`labeler="auto-heuristic-v1"` — heuristic, не human-labeled, але це найкраще, що є).
2. Або згенерувати окремий gold split з ручним labelling — на 44 рядках це 1-2 години роботи.

> Без цього фіксу обговорення Recall@10 vs nDCG@10 vs MRR@10 — теоретичне.

---

## Sanity-fact #2: gold labelling — heuristic, не human

`build_doc_intent_eval.py:220-264` — `expected_paths` генерується через `bm25(chunks)` top-5 + path-token overlap ≥0.5. **Це той самий FTS5 lexical signal, який ми пізніше будемо використовувати у hybrid pipeline.** Bias risk: якщо нова docs-tower модель semantically знайде доc, який BM25 не surface, ми його **не залічимо** — false negative.

`labeler="auto-heuristic-v1"` чесно це позначає у JSONL, але оперувати рекомендаціями моделей на основі цього labelling without dual-judge calibration — небезпечно (memory: `feedback_code_rag_judge_bias.md` — judges never neutral).

**Required:** перед фінальним vote — sample 20 unlabeled top-10 hits per candidate, dual-judge (Opus + MiniLM), порівняти agreement rate vs heuristic gold. Це закладено у `project_docs_model_research_2026_04_24.md` Risk #4.

---

## M1 — Binary Hit@10 vs Proportional Recall@10 (multi-path bias)

Поточна логіка (line 202): `hit = bool(expected & retrieved)` — це **Hit@10 binary**, а не Recall@10. На eval з `expected_paths` length=5 для всіх 44 рядків:

| метрика на one row | визначення | модель X знаходить [A] | модель Y знаходить [A,B,C] |
|---|---|---|---|
| Hit@10 (current code) | `min(1, |E ∩ R|)` | 1 | 1 |
| Recall@10 (proportional) | `|E ∩ R| / |E|` | 0.20 | 0.60 |

**Те, що ми зараз називаємо Recall@10 — насправді Hit@10.** На rows з |E|=5 це активно дискримінує **на користь** моделей, які знайшли хоч щось — ховає реальну якість retrieval.

**Recommendation:** перейти на справжній Recall@10 = `|E ∩ R| / min(|E|, K)`. Це stronger signal саме на multi-path eval (а у нас 100% таких).

Альтернатива: **Recall@K-with-cap = `|E ∩ R| / min(|E|, K)`** — захищає від rows де `|E| > K`. На K=10 і |E|=5 розкид однаковий, але майбутні rows з |E|=12 не пересмажать метрику.

---

## M2 — nDCG@10: rank-aware, але потрібна graded relevance

nDCG@10 врахує **rank** (модель, що знайшла A на rank 1 vs rank 10 — DCG різний). Це дає richer ranking signal. Але:

- **Потрібен graded relevance** (highly-relevant vs partially-relevant). У нас усі `expected_paths` бінарні — relevant=1, not-relevant=0. У такому режимі nDCG@10 ≈ MRR-варіант DCG, корисність обмежена.
- Реалізація: на бінарних мітках `nDCG@10 = (Σ_{i: rank_i in E} 1/log2(rank_i+1)) / IDCG(|E|)`. Тривіально дописати у бенчмарку.

**Verdict M2:** nDCG@10 додавати треба, **але як secondary**. Він буде давати майже-такий-же ranking models як Recall@10 на binary labels. Виграє при graded relevance (TBD — потребує ручного score 1/2/3 на 44 рядки = ~1 година).

---

## M3 — MRR@10: користь обмежена

MRR@10 = `mean over queries of 1/rank_of_first_hit (0 if not in top-10)`. Корисний коли **користувач дивиться лише top-1/top-3**.

Контекст: `mcp_server.py:118-154` — `search()` default `limit=10`, max 20. У нас агент (LLM), не людина — він читає всі 10 результатів, тому MRR over-emphasizes top-1, що **не reflectує** наш use case.

**Verdict M3:** **SKIP** як primary. Залишити як sanity check у dump (вже є — bench має per-query top_files з ranks).

---

## M4 — Precision@5: не відповідає use case

Precision@5 = `|E ∩ top5| / 5`. Корисний для UI з 5 results. Pay-com search UI не існує — користувач через MCP отримує `limit=10` за дефолтом. До того ж на `expected_paths` length=5 Precision@5 топ-обмежений ≤1.0 тільки якщо модель ідеально дала рівно ці 5 (нереально).

**Verdict M4:** **SKIP**. Не reflectує реальний use case (LLM-агент читає top-10).

---

## M5 — Statistical significance на n=44 (paired bootstrap, 10k iter)

Симуляція з baseline=0.45, кореляція моделей ρ=0.7 (paired):

| true lift | average obs Δ | 95% CI half-width | % CIs span 0 |
|---:|---:|---:|---:|
| **+5pp**  | +5pp  | ±5.4pp  | **86% — невидимий** |
| **+10pp** | +10pp | ±8.4pp  | **34% — borderline** |
| **+15pp** | +15pp | ±10.2pp | 7% — significant |
| +20pp | +20pp | ±11.5pp | 0% |
| +25pp | +25pp | ±12.7pp | 0% |

McNemar-approximation:  
SE(Δ) ≈ √(p·(1-p)·2·(1-ρ)/n) ≈ √(0.2475·2·0.3/44) ≈ **0.058**  
MDL@α=0.05 ≈ 1.96·SE ≈ **+11pp**

**Implication:** на n=44 ми **не можемо detect** заявлений у `project_docs_model_research_2026_04_24.md` "+3-5 pp lift" від nomic-v2-moe vs v1.5. Якщо всі 4 кандидати продемонструють Δr@10 у діапазоні ±10pp — **це може бути noise**, не сигнал.

> Це блокує prematire deployment decision у ROADMAP "if ≥3pp Recall@10 lift, deploy without testing other candidates" — на n=44 +3pp не reliable signal.

**Recommendation для T3 (significance teammate):**
- **Мінімальний eval size для +5pp signal at 80% power, α=0.05:** n ≥ 200 paired queries (rough McNemar estimate).
- Якщо n=44 фіксований — promote thresholds мають бути **±15pp absolute**, не ±3pp.

---

## M6 — Multi-metric scoreboard (anti-cherry-pick)

Замість one-number — таблиця per model:

| Model | Recall@10 (proportional) | Hit@10 (binary) | nDCG@10 (binary) | MRR@10 | n queries hit | wall enc s |
|---|---:|---:|---:|---:|---:|---:|
| docs (nomic-v1.5)    | … | … | … | … | … | … |
| docs-nomic-v2-moe    | … | … | … | … | … | … |
| docs-gte-large       | … | … | … | … | … | … |
| docs-arctic-l-v2     | … | … | … | … | … | … |
| docs-bge-m3-dense    | … | … | … | … | … | … |

**Decision rule (anti-cherry-pick):**
1. **Primary gate:** ΔRecall@10 (proportional) ≥ +0.10 (надмінімальної детекції бар, відображає p≤0.05 на n=44).
2. **Co-gate:** ΔnDCG@10 ≥ +0.05 (rank-quality має не регресувати).
3. **Hit@5 floor:** Hit@5 не падає на >5pp (UI experience).
4. **Per-stratum guard:** жоден з 9 страт (`payout / provider / nuvei / webhook / method / interac / refund / trustly / aircash`) не має `Δ < -0.15` (захист від bias до однієї domain).
5. **No tied-rank flips:** якщо різниця між двома кандидатами на primary = top — підняти dual-judge sample на топ-50 disagreement pairs, перевірити agreement (memory: `project_p1b_opus_judge_verdict.md`).

Якщо ці 5 умов нон-одночасно виконуються — verdict = **HOLD**, не promote. Замість cherry-pick "виграв на metric X" → треба **dominance** на 3+ метриках одночасно.

---

## M7 — Vector-only vs end-to-end with reranker

`benchmark_doc_intent.py:6-8` явно каже:
> Bypasses router (`src/search/hybrid.py::_query_wants_docs`) and reranker (`reranker_ft_gte_v8`) — measures the raw vector tower in isolation. That is the only fair signal for "is the new docs model better?"

`hybrid.py:436-646` — production pipeline це FTS top-150 + vector top-50 + RRF fusion + code_facts/env_vars boosts + content boosts + CrossEncoder rerank (70% rerank + 30% RRF) + penalties + sibling expansion + similar-repo annotation. Reranker тут **домінуючий signal** (70% ваги).

**Risk #1 (cardinal):** docs model який виграє vector-only може **програти end-to-end** якщо її embedding distribution погано грає з cross-encoder normalization. І навпаки — модель з гіршим vector retrieval може виграти, якщо її top-50 краще перетинається з reranker's preferred docs.

**Risk #2:** router (`_query_wants_docs`) **визначає, чи модель docs-tower взагалі викликається**. У production hybrid, чисто-doc-intent запит → docs tower only; mixed → fan-out обох. Bypassing router у бенчі — ми міряємо upper-bound, не actual prod recall.

**Recommendation:**
- Залишити vector-only як **primary** — це чистий компонентний test (model-comparison signal).
- Додати **complementary end-to-end run** на тих же 44 запитах: hybrid_search(query, docs_index=True) → top-10 → recall@10. Якщо ranking моделей **не співпадає** vector-only vs end-to-end — це red flag і вимагає reranker A/B re-test (не входить у поточний scope).
- Додати **router pass-rate**: яка частина 44 запитів насправді triggered `_query_wants_docs=True` у production? Якщо <50% — нашa eval set bias до doc-intent перевищує реальний production traffic.

---

## Sanity-fact #3: eval set bias risk (memory: `project_docs_production_analysis_2026_04_24.md`)

З memory:
> 46.8% prod searches doc-intent, avg query 4.71 tok (long-ctx wasted), 98% English, eval set BIASED (9/30 top-term overlap). Confirms gte-large pick + must expand eval set with 30-50 prod-sampled queries.

**Поточний eval (44 рядки) уже sampled з prod logs (`build_doc_intent_eval.py:149-178`)**, тому prod-bias claim тут недоречний. Але:

- `expected_paths` length=5 для 100% rows → це **не** реальний production distribution. Реально юзер шукає **один** канонічний doc, не 5. Multi-path expectation надає bonus моделям з **широким** retrieval (recall-leaning), penalize моделі з **сфокусованим** retrieval (precision-leaning). Якщо prod use case — "знайди той самий doc, що я раніше відкривав", precision важливіша, і нашa Recall@10 буде misleading.

**Recommendation:** додати **per-query gold cardinality** як stratum (1-2 paths vs 3-5 paths). Якщо моделі сильно розходяться між цими stratums — це сигнал, що "average Recall@10" приховує important behavior.

---

## Підсумок рекомендацій (для synthesis)

### MUST-FIX перед запуском
1. **F1.** Полагодити `benchmark_doc_intent.py` — підтримати `expected_paths` (зараз ловить тільки `expected_files` на `gold=True` rows; обидва відсутні). 1-line change або повний rewrite read-loop.
2. **F2.** Перейменувати поточний "Recall@10" → "Hit@10" у вихідному JSON (це binary), додати справжній Recall@10 (proportional).
3. **F3.** Розширити summary table з one-column до multi-metric (Hit@10, Recall@10, nDCG@10, MRR@10).

### NICE-TO-HAVE
4. **F4.** Додати end-to-end run (hybrid_search docs_index=True) як sanity для M7.
5. **F5.** Додати per-stratum breakdown у summary.
6. **F6.** Smoke-test cos-similarity на 5 hand-picked pairs ПЕРЕД full eval (методологія `project_docs_model_research_2026_04_24.md` §"Smoke test").

### MUST-RAISE THRESHOLD
7. **F7.** Promote threshold = **ΔRecall@10 ≥ +0.10 AND ΔnDCG@10 ≥ +0.05 AND no stratum Δ < -0.15** на n=44. Поточний "≥3pp" з memory — нижче detection floor (+5pp = 86% CI span 0).

### TBD на пізніше
8. **F8.** Розширити eval set до n≥200 щоб detection floor спустити до +5pp. На це 30-60 хв ручної праці (auto-resolve через build_doc_intent_eval з PER_STRATUM=10 + OTHER_SLOTS=20 → ~110 candidates, далі manual review).
9. **F9.** Dual-judge calibration (Opus + MiniLM) на 20 candidate top-10 hits per model для верифікації auto-heuristic gold (per `project_docs_model_research_2026_04_24.md` Risk #4).

---

## Recommended metric set (final)

| Role | Metric | Threshold | Notes |
|---|---|---|---|
| **Primary gate** | Recall@10 (proportional) | ΔRecall@10 ≥ +0.10 | На n=44 — мінімум для significance |
| **Co-primary** | nDCG@10 (binary) | ΔnDCG@10 ≥ +0.05 | Rank-quality co-signal |
| **Floor guard** | per-stratum Recall@10 | ∀ stratum: Δ ≥ -0.15 | Anti-domain-bias |
| **UI sanity** | Hit@5 (binary) | ΔHit@5 ≥ -0.05 | Top-of-list quality |
| **Latency sanity** | encode_seconds | <2x baseline | Не лінкається до якості, але hard cap |
| Diagnostic | MRR@10, Hit@10 | logged-only | Не gate, для debug |
| Diagnostic | end-to-end Recall@10 (with reranker) | logged-only | M7 risk-mitigation |

Composite single-number score: **NOT recommended** — encourages cherry-picking. Замість цього — multi-metric AND-gate (5 умов вище). Якщо хтось дуже хоче one-number — використати tie-break order: Recall@10 > nDCG@10 > Hit@5.

---

## Status
- **T2 self-assessment:** усі M1-M7 атаковано, висновки конкретні, метрики justified.
- **Blockers для T4 synthesis:**
  - F1 (benchmark не міряє нічого) — high; виправити в одному PR перед запуском A/B.
  - F7 (threshold treatment) — medium; узгодити з team-lead перед deployment criteria draft.
- **Залежність T2 → T4:** synthesis повинна врахувати, що поточний benchmark broken AND threshold floor неправильний AND single-metric підхід призводить до cherry-pick.
