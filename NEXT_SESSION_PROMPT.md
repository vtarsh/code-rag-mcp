# Промпт для автономної сесії — покращення recall@10

> Дата створення: 2026-05-19
> Мета: Покращити recall@10 на eval наборі з 665 JIRA-запитів через точковий аналіз та систематичні фікси

---

## 1. Контекст проєкту

- **Repo**: `~/.code-rag-mcp` (Python 3.12, macOS, 16 GB RAM)
- **Vectors**: 81k rows, LanceDB ~55 GB
- **Eval set**: `profiles/pay-com/eval/jira_eval_clean.jsonl` (665 queries)
- **Models**: CrossEncoder reranker (`Tarshevskiy/pay-com-rerank-l12-ft-run1`), local embeddings
- **Mac limitation**: Daemon crashes after ~85 queries due to multiprocessing semaphore leak

### Поточна архітектура пошуку

```
Query → expand_query() → FTS5 keyword search (150 candidates, 2x weight)
                     → Vector search (LanceDB, 50-100 candidates)
                     → RRF fusion (Reciprocal Rank Fusion, K=40)
                     → Intent routing boosts (frontend/backend repo multipliers)
                     → Reranker pool (top 200)
                     → CrossEncoder rerank (top 10)
```

Ключові файли:
- `src/search/hybrid.py` — RRF fusion, reranker call
- `src/search/fts.py` — FTS5 search, query expansion
- `src/search/service.py` — intent detection, query preprocessing
- `scripts/eval/eval_jira_daemon.py` — eval через daemon API
- `scripts/eval/run_batched_eval.sh` — batched eval з перезапуском daemon

---

## 2. Що вже зроблено (не чіпати без згоди)

### ✅ Закомічено та працює

| Фікс | Файл | Ефект |
|------|------|-------|
| Conservative query expansion (тільки токени ≤6 символів) | `src/search/fts.py` | +1pp hit@10, зупинив регресію -12pp від aggressive expansion |
| Noise exclusion | `CODE_RAG_DEFAULT_EXCLUDE` env | Критично для provider queries |
| Recall@10 tracking | `scripts/eval/eval_jira_daemon.py`, `eval_jira_clean.py` | Тепер міряємо реальну якість |
| Frontend intent detection (word-boundary, plural variants) | `src/search/service.py` | Зменшив false positives |

### 📊 Поточні baseline метрики

| Офсет | Config | hit@10 | recall@10 |
|-------|--------|--------|-----------|
| 0-49 | Baseline (no routing/expansion) | 70.00% | 17.77% |
| 0-49 | Fixed (routing + conservative expansion) | 72.00% | 17.45% |
| 200-249 | Baseline | 64.00% | 14.46% |
| 200-249 | Fixed | 64.00% | 13.64% |

---

## 3. Коренева проблема (прочитати `RERANKER_IMPROVEMENT_PLAN.md`)

**Основна причина низького recall:** Очікувані файли часто НЕ містять ключових слів запиту в своїх назвах і коді. FTS5 знаходить файли, де слова **ЗГАДАНІ** (напр. `authorization/consts.ts` має 39 згадок CRM), а не де функціонал **РЕАЛІЗОВАНИЙ**.

**Два типи проблем:**
1. **Retrieval failure** — expected файл не в пулі кандидатів (топ-200)
2. **Reranker failure** — expected файл у пулі, але поза топ-10

---

## 4. Алгоритм роботи

### Фаза 1: Ручний аналіз перших 50 запитів (offset 0-49)

**Працюй самостійно, НЕ делегуй агентам.**

Для кожного запиту:

1. **Перевірь top-10 results** (через daemon API або direct `hybrid_search()`)
2. **Порівняй з expected files** (з `jira_eval_clean.jsonl`)
3. **Визнач тип проблеми:**
   - 🔴 **Retrieval failure** — expected файлу немає в топ-200
   - 🟡 **Reranker failure** — expected файл у топ-200, але не в топ-10
   - 🟢 **Hit** — expected файл у топ-10

4. **Діагностика retrieval failure:**
   - Чи є expected файл в індексі? (пошук за точною назвою)
   - Чи містить expected файл ключові слова запиту?
   - Чи є схожі файли в тому ж репо, які ЗНАЙШЛИСЯ?
   - Чому vector search не знаходить файл?

5. **Діагностика reranker failure:**
   - На якій позиції у пулі expected файл?
   - Які файли reranker поставив вище?
   - Чому ці файли отримали вищий скор?

6. **Застосуй точковий фікс, якщо можливо:**
   - Додати query expansion для конкретного abbreviation/typo
   - Додати repo boost для конкретного патерну
   - Поліпшити intent detection для конкретного keyword
   - Змінити glossary entry
   
   **ВАЖЛИВО:** Кожен фікс має бути мінімальним і перевіреним на те, що він не ламає інші запити.

7. **Запиши висновок** у `SESSION_FINDINGS.md` (формат нижче)

### Фаза 2: Delegation (offset 50+ через агентів)

Коли знайдеш патерни (після 30-50 запитів):

1. Створи **агентів по 10 запитів кожен** (subagents типу `coder`)
2. Кожен агент отримує:
   - Список своїх 10 запитів
   - Інструкцію з алгоритму (пункти 1-7 вище)
   - Шаблон для запису висновків
   - Команду НІЧОГО не комітити без твоєї перевірки
3. Перевірь результати кожного агента
4. Застосуй тільки ті фікси, які:
   - Не конфліктують між собою
   - Покращують hit@10 або recall@10 на всьому батчі

---

## 5. Формат запису висновків (`SESSION_FINDINGS.md`)

Для кожного запиту записуй:

```markdown
### Query N: "[текст запиту]"

**Expected files:** [N files] — перелік репо + ключові шляхи
**Top-10 results:** перелік репо, які повернулись
**Problem type:** retrieval_failure | reranker_failure | hit

**Diagnosis:**
- [Чому не знайшлось? Конкретна причина]
- [Які файли reranker поставив вище і чому?]

**Fix applied:** [що змінено, якщо застосовано фікс]
**Before fix:** hit@10=X%, recall@10=Y%
**After fix:** hit@10=X%, recall@10=Y%

**Training data note:**
- Positive pair: (query, expected_file) — [чому релевантно]
- Hard negative: (query, wrong_file) — [чому reranker помиляється]
```

---

## 6. Як працювати з eval

### Запуск eval на батчі (через daemon)

**ВАЖЛИВО:** Daemon може бути "отруєний" — перевірь env перед стартом:

```bash
# Перевірка чистоти демона
curl -s http://127.0.0.1:8742/health
# Якщо daemon працює, вбий його і запусти чистий:
pkill -f "python3 daemon.py"

# Запуск чистого daemon
export CODE_RAG_HOME="/Users/vaceslavtarsevskij/.code-rag-mcp"
export ACTIVE_PROFILE=pay-com
export CODE_RAG_DEFAULT_EXCLUDE="package_usage,provider_doc,dictionary"
export CODE_RAG_RRF_K=40
export CODE_RAG_KEYWORD_WEIGHT=2.0
export CODE_RAG_DISABLE_DOCS_TOWER=1
export CODE_RAG_CODE_RERANKER=Tarshevskiy/pay-com-rerank-l12-ft-run1
export CODE_RAG_IDLE_UNLOAD_SEC=0
python3 daemon.py &

# Очікування
curl -s http://127.0.0.1:8742/health
```

### Запуск eval на підмножині

```bash
python3 scripts/eval/eval_jira_daemon.py \
  --out=bench_runs/session_batch_X.json \
  --offset=0 \
  --count=10
```

### Batched eval на великих діапазонах (>50 queries)

```bash
# Використовуй run_batched_eval.sh — він сам перезапускає daemon кожні 50 queries
bash scripts/eval/run_batched_eval.sh
```

---

## 7. Як працювати з агентами

```python
# Приклад створення агента для батчу 10 запитів
agent = Agent(
    description="Analyze batch of 10 queries",
    prompt="""
    Analyze these 10 queries from the eval set and find why recall is low.
    For each query:
    1. Call hybrid_search() and inspect top-20 results
    2. Compare with expected files from jira_eval_clean.jsonl
    3. Determine if it's retrieval failure or reranker failure
    4. Suggest minimal fixes
    
    DO NOT commit anything. Write findings to a markdown file.
    
    Queries: [list of 10 queries with their expected files]
    """,
    subagent_type="coder"
)
```

---

## 8. Обмеження та правила

| Обмеження | Що робити |
|-----------|-----------|
| Daemon падає після ~85 queries | Запускай eval батчами по 50, з перезапуском daemon |
| 16 GB RAM | НЕ запускай кілька Python процесів з моделями одночасно |
| Не коміть без тесту | Кожен фікс має бути перевірений на 10-50 запитах |
| Не ламай baseline | Якщо фікс покращує 1 запит, але погіршує 3 — відкоть |

---

## 9. Ключові файли для редагування

| Файл | Призначення |
|------|-------------|
| `src/search/fts.py` | Query expansion, FTS5 sanitization |
| `src/search/hybrid.py` | RRF fusion, intent boosts, reranker call |
| `src/search/service.py` | Intent detection (_FRONTEND_KEYWORDS, _BACKEND_KEYWORDS) |
| `profiles/pay-com/glossary.yaml` | Domain glossary for query expansion |
| `scripts/eval/eval_jira_daemon.py` | Eval script (вже має recall@10) |
| `RERANKER_IMPROVEMENT_PLAN.md` | План для навчання моделі (читати!) |
| `SESSION_FINDINGS.md` | [СТВОРИТИ] Твої висновки з цієї сесії |

---

## 10. Перші кроки для цієї сесії

1. **Прочитай** `RERANKER_IMPROVEMENT_PLAN.md` — там детальний аналіз проблем
2. **Створи** `SESSION_FINDINGS.md` для запису висновків
3. **Запусти eval** на offset 0-9 (перші 10 запитів) з fixed config:
   ```bash
   # Fixed config env vars
   export CODE_RAG_HOME="/Users/vaceslavtarsevskij/.code-rag-mcp"
   export ACTIVE_PROFILE=pay-com
   export CODE_RAG_DEFAULT_EXCLUDE="package_usage,provider_doc,dictionary"
   export CODE_RAG_RRF_K=40
   export CODE_RAG_KEYWORD_WEIGHT=2.0
   export CODE_RAG_DISABLE_DOCS_TOWER=1
   export CODE_RAG_CODE_RERANKER=Tarshevskiy/pay-com-rerank-l12-ft-run1
   export CODE_RAG_IDLE_UNLOAD_SEC=0
   export CODE_RAG_FRONTEND_BOOST=1.3
   export CODE_RAG_FRONTEND_DEMOTE=0.9
   export CODE_RAG_BACKEND_BOOST=1.05
   export CODE_RAG_USE_EXPAND_QUERY=1
   
   python3 daemon.py &
   python3 scripts/eval/eval_jira_daemon.py --out=bench_runs/session_start_0_9.json --offset=0 --count=10
   ```
4. **Проаналізуй кожен запит** — чому recall низький, що можна виправити
5. **Застосуй фікси** і повтори eval
6. **Запиши висновки** в `SESSION_FINDINGS.md`

---

## 11. Приклад очікуваного результату

Після сесії ми хочемо мати:

- `SESSION_FINDINGS.md` з детальним аналізом 50-100+ запитів
- Список застосованих фіксів з метриками before/after
- Набір training pairs для reranker'а (positive + hard negatives)
- Чітке розуміння, які патерни найчастіші (для масштабування через агентів)

---

**Успіхів! Не поспішай — краще 10 глибоко проаналізованих запитів, ніж 100 поверхнево.**
