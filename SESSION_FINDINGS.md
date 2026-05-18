# SESSION_FINDINGS — Висновки з аналізу запитів

> Сесія: [дата]
> Опрацьовано запитів: 0
> Покращень: 0

---

## Метрики до початку сесії

| Config | hit@10 | recall@10 |
|--------|--------|-----------|
| Baseline (offset 0-49) | 70.00% | 17.77% |
| Baseline (offset 200-249) | 64.00% | 14.46% |
| Fixed config (offset 0-49) | 72.00% | 17.45% |
| Fixed config (offset 200-249) | 64.00% | 13.64% |

---

## Патерни проблем (заповнювати по ходу)

### Патерн 1: [назва]
- Частота: [скільки запитів з 50]
- Опис: [коротко]
- Fix: [що допомогло]

---

## Детальний аналіз запитів

<!-- Копіювати шаблон для кожного запиту -->

### Query N: "[текст запиту]"

**Expected files:** [N files]
**Top-10 results:** [репозиторії]
**Problem type:** retrieval_failure | reranker_failure | hit

**Diagnosis:**
- 

**Fix applied:**
- 

**Before fix:** hit@10=X%, recall@10=Y%
**After fix:** hit@10=X%, recall@10=Y%

**Training data note:**
- Positive pair: (query, expected_file) — 
- Hard negative: (query, wrong_file) — 

---

## Застосовані фікси (summary)

| # | Фікс | Файл | Запитів покращено | Запитів погіршено |
|---|------|------|-------------------|-------------------|
| 1 | | | | |

---

## Training pairs (для reranker'а)

### Positive pairs (label=1)
```
Query: "..."
File: repo | path
Reason: 
```

### Hard negatives (label=0)
```
Query: "..."
File: repo | path
Reason: reranker ставить це вище, але це не правильна відповідь
```

---

## Рекомендації для наступних сесій / агентів

1. 
