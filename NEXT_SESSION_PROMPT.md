# Prompt for next session (copy-paste below)

---

Продовжую `code-rag-mcp` після нічного прогону (2026-04-21). **Перед будь-якою дією прочитай повністю `ROADMAP.md`** — особливо §"🌙 2026-04-21 overnight" та §"Where we think we should go next".

Також прочитай пам'ять: `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/MEMORY.md` та всі файли за посиланнями.

## Поточний стан (головне)

- **У проді: `reranker_ft_gte_v8`** (listwise LambdaLoss, 285MB bf16). Swap зроблений 2026-04-21, config.json абсолютний шлях. Працює.
- **11 FT ітерацій досягли ceiling +3-5pp Jira r@10.** Далі rerank-тюнінг — diminishing returns.
- **Тільки v8 реально покращив runtime benchmarks** (+8.3pp queries, +2.1pp realworld). v6.2, v10, v11 = tie з baseline на runtime. Jira r@10 gains НЕ транслюються в реальні MCP queries.
- **Daemon на `:8742` тримає ~2.7-2.8GB** (CodeRankEmbed 1GB + LanceDB mmap 500MB-1GB + reranker 400MB + runtime). Це постійне, не залежить від reranker vendor.

## Головне питання сесії

**"Де далі шукати реальне покращення якості пошуку, якщо rerank FT витиснений?"**

Rerank — polish. Recall bottleneck = FTS5+dense stage. Real breakthrough має бути **поза reranker'ом**.

## Що НЕ робити

- Не тренуй ще одну rerank FT без чіткого falsification plan. 11 ітерацій вже вичерпали простір.
- Не спамуй hyperparam sweep (lr/batch/loss) — сезі запущено.
- Не міняй `profiles/pay-com/config.json` без explicit user ask.
- Не push через `gh` — тільки `mcp__github__*`, owner=vtarsh.

## Перший крок — ОБОВ'ЯЗКОВО

Запусти **3-5 паралельних критиків-агентів** (`general-purpose`, `model: opus`) щоб перевірити які untried axes реально мають потенціал ДО будь-яких дій. Конкретні теми:

1. **Query rewriting — реальний impact на нашу recall метрику?** Прочитай existing Jira eval snapshots (`gte_v1.json` baseline); для скількох tickets GT файл взагалі НЕ у top-200 FTS+dense? Якщо <10%, query rewrite має низьку стелю. Якщо >30%, це найбільший lever.

2. **Dense retrieval FT — варто зробити CodeRankEmbed FT?** Прочитай `scripts/build_vectors_coderank.py`, `src/search/vector.py`. Є можливість FT embedding model на наших (query, chunk, label) парах? Estimated effort? Community recipe?

3. **v8 + v10 ensemble score-average.** v10 видалений локально, але є `gte_v10.json`. Агент має sanity-check — чи передбачувані per-ticket errors v8/v10 uncorrelated (clean pairing). Якщо так — ensemble ймовірно +1-2pp за дешево. Якщо errors correlated — скіп.

4. **Real-query eval з `logs/tool_calls.jsonl`.** 1,194 unique queries з production. Скільки labeled для ground-truth треба щоб eval був statistically meaningful? LLM-as-judge чи manual? Cost estimate.

5. **Bugs + technical debt.** Прочитай git diff, daemon.py, pre-commit hook (зараз flakey під MPS навантаженням). Шукай:
   - `fts_index.db` — 0 байтів, dead file. Видалити?
   - `knowledge.db.bak-p7` (149MB) — старий бекап. Потрібен?
   - Pre-commit pytest падає коли daemon тримає MPS — repeatable? fixable (split test suite, lighter hook)?
   - `knowledge.db` (177MB) vs `.bak-p7` (149MB) — різниця актуальна?

Запусти всі 5 паралельно (single message, multiple `Agent` tool calls), `model: opus`, чекай усі, синтез, потім пропонуй конкретну дію.

## Правила роботи

- **Маленькими кроками**: написати → verify → commit → далі.
- **Checkpoint commit** після кожного значущого кроку.
- **Tests must pass** перед push: `python3.12 -m pytest tests/ -q` (зараз 325).
- **Push через `mcp__github__push_files`** (owner=vtarsh, repo=code-rag-mcp).
- **Pre-commit pytest hook** flakey під MPS — якщо падає з "11 errors" під час training/eval background job, це false positive. Перевір manually, тоді commit.
- **Перед тренуванням** — MANDATORY sample check (5 train/5 test rows, visual compare).
- **Real holdout ОБОВ'ЯЗКОВО** — `--test-ratio 0.15` для v12+. Full-eval з train-in-test = memorization-inflated (цей урок коштував нам 11 ітерацій).
- **Jira r@10 gains ≠ runtime gains.** Завжди перевіряй `benchmark_queries.py` + `benchmark_realworld.py` окремо.

## Proven FT recipe (якщо все ж треба тренувати щось нове)

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

Don't use `--dedupe-same-file` (v5 catastrophe). Don't use `lr=8e-5 + batch=16` (v11 CORE overfits).

## Critical pitfalls (коротко, full list в ROADMAP)

1. Rerank FT ceiling = +3-5pp on Jira, ~nothing on runtime (except listwise LambdaLoss v8).
2. Real-holdout MANDATORY (`--test-ratio 0.15`).
3. `max_length=256 + batch=32` OOMs MPS. Use batch=16.
4. FTS5 `_FTS_PRECLEAN` must strip all non-word/space/.-/ punctuation.
5. Reranker path у config.json ABSOLUTE (daemon cwd ≠ repo).
6. Pre-commit pytest падає під MPS контенцією — не зупиняйся через це, verify manually.

## Початковий запит (копіпастуй у новий сеанс)

```
Прочитай ROADMAP.md + memory. Поточний prod = v8, rerank витиснений. Запусти 5 критиків-агентів паралельно на 5 питань із секції 'Перший крок' у NEXT_SESSION_PROMPT.md. Зроби синтез ДО будь-якої дії. Фокус — де шукати real breakthrough поза rerank FT. Без коду до синтезу. Після синтезу — працюй автономно з checkpoints, не чекай підтвердження на кожну дрібницю.
```
