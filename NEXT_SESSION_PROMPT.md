# Prompt for next session (copy-paste below into new Claude Code session)

---

Продовжую P5 reranker у `code-rag-mcp`. Перед будь-якою дією **прочитай повністю `ROADMAP.md`** — там вся історія 8 FT ітерацій (v1→v8), що зараз у прод, P0 fix gate (DONE), Graph POC (FAILED), v8 listwise (PROMOTE sidegrade), і критично — **новий пріоритет "якість > швидкість"**.

Також прочитай пам'ять: `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/MEMORY.md` + всі файли за посиланнями.

## Вектор напрямку

**Головне питання сесії:** "чи v6.2 / v8 справді варто поставити в прод, чи вони виграють тільки на Jira eval?"

Новий пріоритет користувача: **якість важливіша за швидкість**. Попередні сесії тримали v6.2/v8 у архіві через 2× latency — **ця причина більше не головна**. Якщо модель справді краща на runtime query distribution + не має схованих per-project регресій — deploy.

## Що НЕ робити одразу

Не тренуй нову модель, не чіпай `src/config.py`, не свопай reranker. Ми вже 8 разів повторювали одну помилку: діяли на непідтверджених гіпотезах.

## Перший крок — ОБОВ'ЯЗКОВО

Запусти **3-5 паралельних критиків-агентів** (`general-purpose`, `model: opus`) щоб перевірити ключові claims ПЕРЕД дією. Конкретні теми:

1. **Runtime benchmarks — чи v6.2 і v8 справді кращі на `benchmark_queries.py` + `benchmark_realworld.py`?**
   Критик має запустити обидва benchmarks (окремо на v6.2 та v8), порівняти з baseline (ms-marco-MiniLM-L-6-v2), і report: win/tie/lose кожен з 2 бенчмарків, на кожній моделі. Попередній audit казав "mixed or worse" — перевір чи це ще так після fix gate.

2. **Per-project parity у Jira eval** — break down `gte_v6_2.json` і `gte_v8.json` по prefix (PI / BO / CORE / HS). Кожен проект net-wins? Чи CORE регресує а BO домінує aggregate? Critic має compute per-project Δr@10 + ΔHit@5 + net з existing snapshot data (no training).

3. **v8 vs v6.2 — який правильний для MCP use-case?** Current pipe: user calls `search` MCP tool → отримує top-10 results → часто дивиться top-5. v6.2 краще на r@10, v8 краще на Hit@5. Критик має перевірити ЯКИЙ фактичний top-K використовує MCP (read `mcp_server.py` + `src/search/hybrid.py`), і порекомендувати.

4. **Чи наш новий gate (`Δr@10 ≥ 0.02 AND ΔHit@5 ≥ 0.02 AND net_improved ≥ 20`) достатньо надійний для prod-decision?** Або треба додаткові guards (e.g. "no project below baseline", "variance-checked re-runs")? Критик — adversarial, шукає pathologies які gate пропустить.

5. **Чи є ще один untried lever (post-audit) який дав би clear upgrade над v6.2/v8?** Перевір fresh, не покладайся на попередні recommendations. Кандидати з ROADMAP: freeze bottom 6 layers, dense-neighbor hard negatives, v8+longer max-length на GPU.

Запусти agents паралельно (single message, multiple `Agent` tool calls), **кожен з `model: opus`**, чекай результати, потім синтез. **Не запускай train/deploy до синтезу.**

## Після синтезу

На базі висновків критиків:
- Якщо v6.2 (або v8) passes runtime benchmarks І per-project parity OK → **deploy** (змінити `profiles/pay-com/config.json::reranker_model` + restart daemon). Дочекайся validation.
- Якщо не passes → вирішуй: або інший lever (freeze layers / dense negs), або real-query eval.

## Правила роботи (автономність з checkpoints)

- **Маленькими кроками:** спочатку написати код → перевірити напрямок → commit → далі.
- **Checkpoint commit після кожного значущого кроку** (не накопичувати). Якщо щось ламається — відкат до попереднього commit.
- **Tests must pass** перед push: `python3.12 -m pytest tests/ -q` (зараз 316).
- **Push тільки через `mcp__github__*`** (`gh` deny-listed). Owner = `vtarsh`.
- **Train/eval параметри** — див. ROADMAP §"Proven settings" та §"Critical pitfalls".
- **Shard eval** — тепер балансується per-prefix (CORE/BO/PI/HS) автоматично у `eval_finetune.py`. `EVAL_BATCH=2 EVAL_MAXLEN=256` у `eval_parallel.sh` — не міняй.

## Production state (коротко)

- Reranker у проді: `cross-encoder/ms-marco-MiniLM-L-6-v2` (22M, baseline).
- Archive models (не видаляти, всі PROMOTE по Jira gate):
  - `reranker_ft_gte_v4/` — +4.06pp r@10
  - `reranker_ft_gte_v6_2/` — +4.30pp r@10 (best r@10)
  - `reranker_ft_gte_v8/` — +3.92pp r@10, +6.9pp Hit@5 (best top-5), listwise LambdaLoss
- 3 datasets (v4, v6.2 pointwise, v8 listwise), 5 eval snapshots.

## Початковий запит (копіпастуй)

"Прочитай `ROADMAP.md` + memory. Запусти 3-5 критиків-агентів паралельно на 5 claims із секції 'Перший крок' в `NEXT_SESSION_PROMPT.md`. Зроби синтез і тільки тоді пропонуй наступну дію. Без коду до синтезу. Після синтезу — працюй автономно з checkpoints (commit на кожному значущому кроці), не чекай підтвердження на кожну дрібницю."
