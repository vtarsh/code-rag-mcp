# План покращення recall@10 — висновки з аналізу

> Дата: 2026-05-19
> Автор: Claude Code analysis session
> База: 665 JIRA queries, pay-com profile, 81k vectors

---

## 1. Поточний baseline (після conservative expansion fix)

| Метрика | Значення | Примітка |
|---------|----------|----------|
| hit@10 | ~64-68% | Залежить від офсету (0-49: 70%, 200-249: 64%) |
| recall@10 | ~14-18% | Дуже низько — тільки ~1 файл з 6-7 очікуваних у топ-10 |

**Ключовий інсайт:** hit@10 — груба метрика. recall@10 показує, що система пропускає більшість релевантних файлів.

---

## 2. Що випробували і що спрацювало

### ✅ Спрацювало

| Фікс | Ефект | Статус |
|------|-------|--------|
| Conservative query expansion (тільки ≤6 символів) | +1pp hit@10, зупинив регресію -12pp | Закоммічено |
| Noise exclusion (`CODE_RAG_DEFAULT_EXCLUDE`) | Критично для provider queries | Закоммічено |
| Recall@10 tracking в eval | Тепер можемо міряти реальну якість | Закоммічено |

### ❌ Не спрацювало

| Фікс | Чому не спрацювало |
|------|-------------------|
| Intent routing (frontend/backend boost 1.3x/1.05x) | Множники занадто слабкі, щоб перебити RRF-розриви. Також помилкові класифікації ("CRM" → backend, хоча задача в backoffice-web) |
| File path keyword boost (1.3^x) | Очікувані файли НЕ містять ключових слів у назвах (SpecialComponents, eu.tsx, FullScreenModal) |
| Query expansion з довгими тезаурусами | `graphql` → 5 слів, `country` → 4 слова — додавали шум і ламали precision FTS5 |

---

## 3. Коренева причина низького recall

**Проблема НЕ в reranker'і (тільки частково). Проблема в retrieval.**

### 3.1 Semantic mismatch

Очікувані файли для багатьох запитів **не містять ключових слів запиту** в назвах і коді:

| Запит | Очікуваний файл | Чого НЕ містить |
|-------|-----------------|-----------------|
| "CRM Fields Refactor" | `SpecialComponents.tsx` | CRM, entity, fields |
| "Document Preview" | `FullScreenModal.tsx` | document, preview |
| "Document Preview" | `PDFViewer.tsx` | document, preview |
| "VAT Handling" | `mapEuLocalStateToGql.ts` | vat (тільки 3 згадки) |
| "Cloudflare Sessions" | `App.tsx` | cloudflare, session |

FTS5 знаходить файли, де слова **ЗГАДАНІ** (напр. `authorization/consts.ts` має 39 згадок CRM/entity/merchant), а не де функціонал **РЕАЛІЗОВАНИЙ**.

### 3.2 Reranker bottleneck

Коли очікуваний файл ВЖЕ є в пулі кандидатів (топ-200), reranker часто його витісняє:

| Запит | Expected file | Ранг у пулі | Чому reranker пропускає |
|-------|---------------|-------------|------------------------|
| "Document Preview" | `PreviewModal.tsx` | #12-15 | Reranker ставить загальні сторінки документів вище за специфічні компоненти |
| "VAT Input PayPass" | `VatNumberInputField.tsx` | #6 | Загальні валідатори (phoneNumberValidator) отримують вищий скор |

### 3.3 Embeddings не розуміють task-level semantics

Vector search НЕ повертає `FullScreenModal` для "document preview", НЕ повертає `SpecialComponents` для "CRM fields" — тому що embedding модель не навчена розуміти, що ці файли семантично пов'язані з запитами.

---

## 4. Що потрібно для навчання embeddings (code tower)

### Проблема поточної моделі

Поточна модель (coderank або подібна) навчена на generic code semantics. Вона розуміє синтаксичну близькість (`validateVatNumber` ≈ `checkVatNumber`), але НЕ розуміє **task-level** семантику (`FullScreenModal` ≈ "document preview UI component").

### Потрібно

1. **Дообучити embeddings з контекстом**:
   - Додавати `repo_name` + `file_path` до text, який embedдиться
   - Або використовувати модель, яка враховує шлях файлу
   - Приклад: текст для embedding = `[repo_name] [file_path] [function_signatures] [snippet]`

2. **Training data для embeddings**:
   - Positive pairs: (query, expected_file) — де expected файл НЕ містить ключових слів запиту
   - Hard negatives: файли, які містять ключові слова, але НЕ є правильними
   - Приклад hard negative: `authorization/consts.ts` для "CRM Fields Refactor" — файл містить "CRM_FIELDS" константу, але це НЕ реалізація

---

## 5. Що потрібно для навчання reranker'а (CrossEncoder)

### Проблема поточного reranker'а

Поточний reranker (`Tarshevskiy/pay-com-rerank-l12-ft-run1`) отримує:
- query text
- repo_name + file_path + snippet

Він НЕ розуміє, що `PreviewModal.tsx` — це component для preview, а `BusinessDocuments.tsx` — це сторінка, яка використовує preview.

### Потрібно

1. **Покращити input для reranker'а**:
   - Додати function/class names з файлу
   - Додати import statements (показують, з чим пов'язаний файл)
   - Додати repo_description або repo tags

2. **Training pairs (критично)**:
   
   **Positive pairs** (query, expected_file, label=1):
   ```
   Query: "Fix Multi-Page Document Preview in Backoffice"
   File: backoffice-web | src/Components/PreviewModal/PreviewModal.tsx
   Label: 1
   ```
   
   **Hard negative pairs** (query, wrong_file, label=0):
   ```
   Query: "Fix Multi-Page Document Preview in Backoffice"
   File: backoffice-web | src/Pages/Compliance/.../BusinessDocuments.tsx
   Label: 0
   
   Query: "Fix Multi-Page Document Preview in Backoffice"
   File: backoffice-web | src/Components/PreviewButton/functions.ts
   Label: 0
   ```
   
   **Ключове:** hard negatives мають бути файлами з того ж репо, які reranker ЗАРАЗ ставить вище за expected файл.

---

## 6. Конкретний план збору training data

### Крок 1: Зібрати "near-miss" queries (200-300 штук)

Запити, де:
- Expected файл є в пулі кандидатів (топ-200), АЛЕ
- Expected файл НЕ в топ-10 (reranker пропустив)

**Як зібрати:**
```python
# Для кожного запиту в eval
ranked, _, total = hybrid_search(query, limit=200)
expected_set = {(ep.repo, ep.path) for ep in expected_paths}

for i, r in enumerate(ranked, 1):
    if (r.repo_name, r.file_path) in expected_set:
        if i > 10:
            # Це "near-miss" — файл знайдений, але reranker витіснив
            save_for_training(query, r, rank=i)
```

### Крок 2: Для кожного near-miss зібрати hard negatives

Hard negatives = топ-10 файлів, які reranker поставив ВИЩЕ за expected файл.

```python
expected_in_pool = [r for r in ranked if (r.repo, r.path) in expected_set]
top_10_wrong = [r for r in ranked[:10] if (r.repo, r.path) not in expected_set]

for expected in expected_in_pool:
    for wrong in top_10_wrong:
        save_training_pair(query, expected, label=1)
        save_training_pair(query, wrong, label=0)
```

### Крок 3: Додати "retrieval failure" queries (100-200 штук)

Запити, де expected файл НЕМАЄ в пулі кандидатів (топ-200).

Для цих запитів потрібен інший підхід — дообучення embeddings.

```python
for expected in expected_set:
    if expected not in [(r.repo, r.path) for r in ranked]:
        # Retrieval failure — embeddings не знайшли файл
        save_for_embedding_training(query, expected_file, label=1)
        # Hard negative: найближчий файл у vector space, який НЕ є правильним
```

### Крок 4: Ручна анотація 100-200 складних запитів

Найважливіші запити для ручної перевірки:
- Запити з recall=0 (expected файли не знайдені)
- Запити з 20+ expected файлами (великі задачі)
- Запити, де top-10 містить тільки файли з одного репо (монополія)

Для кожного запиту ручно визначити:
- Чи всі expected файли дійсно релевантні?
- Чи є в топ-10 файли, які мають бути expected, але не позначені?
- Чому expected файли не знайшлися?

---

## 7. Приклади training pairs для конкретних запитів

### Приклад 1: "Fix Multi-Page Document Preview in Backoffice"

**Expected файли:**
- `backoffice-web/src/Components/FullScreenModal/FullScreenModal.tsx`
- `backoffice-web/src/Components/PDFViewer/PDFViewer.tsx`
- `backoffice-web/src/Components/PreviewButton/PreviewButton.tsx`
- `backoffice-web/src/Components/PreviewModal/PreviewModal.tsx`
- `graphql/src/resolvers/documents/queries/document-download.ts`

**Positive pairs:**
```
Query: Fix Multi-Page Document Preview in Backoffice
Context: [repo: backoffice-web] [path: src/Components/PreviewModal/PreviewModal.tsx]
         [imports: PreviewFile, DocumentUploadStatus, useGetDocumentUrlLazyQuery]
Label: 1
```

**Hard negatives ( reranker зараз ставить вище ):**
```
Query: Fix Multi-Page Document Preview in Backoffice
Context: [repo: backoffice-web] [path: src/Pages/Compliance/.../BusinessDocuments.tsx]
         [imports: useGetBankAccountsQuery, BankAccountType.Settlement]
Label: 0  # Це сторінка документів, НЕ компонент preview

Query: Fix Multi-Page Document Preview in Backoffice
Context: [repo: backoffice-web] [path: src/Pages/Disputes/.../DisputeEvidenceDocs.tsx]
         [imports: useGetDisputeQuery, createEvidenceRows]
Label: 0  # Evidence docs, не preview component
```

### Приклад 2: "CRM Fields Refactor for Entity and Merchant Support"

**Expected файли:**
- `backoffice-web/src/Components/SpecialComponents/SpecialComponents.tsx`
- `backoffice-web/src/Pages/Compliance/MerchantUnderwritingPage/...`
- (41 файл у backoffice-web + 12 в backend)

**Positive pair:**
```
Query: CRM Fields Refactor for Entity and Merchant Support
Context: [repo: backoffice-web] [path: src/Components/SpecialComponents/SpecialComponents.tsx]
         [exports: ReportConfigLabel, Tagged, TestMerchant, MerchantApplicationStatuses]
Label: 1
```

**Hard negative:**
```
Query: CRM Fields Refactor for Entity and Merchant Support
Context: [repo: backoffice-web] [path: src/authorization/consts.ts]
         [content: CRM_FIELDS = 'crm_fields', ENTITY_MCCS = 'entity_mccs', ...]
Label: 0  # Містить слова CRM/entity, але це permissions, не реалізація
```

### Приклад 3: "Refactor VAT Number Handling in PayPass and Backoffice"

**Expected файли:**
- `paypass-web/src/hooks/queries.generated.ts`
- `paypass-web/src/mappers/mapEuLocalStateToGql.ts`
- `paypass-web/src/mappers/mapGqlToEuLocalState.ts`
- `paypass-web/src/renderConfig/eu.tsx`

**Positive pair:**
```
Query: Refactor VAT Number Handling in PayPass and Backoffice
Context: [repo: paypass-web] [path: src/renderConfig/eu.tsx]
         [imports: VatNumberInputField, generalInformation.vatNumber]
Label: 1
```

**Hard negative:**
```
Query: Refactor VAT Number Handling in PayPass and Backoffice
Context: [repo: grpc-onboarding-merchant] [path: libs/integrations/validate-vat-number.ts]
         [imports: viesClient, InvalidDataError]
Label: 0  # Backend validation logic, not PayPass frontend handling
```

---

## 8. Технічні деталі для реалізації

### Поточна архітектура reranker input

```python
# src/search/hybrid_rerank.py
texts = [f"{repo_name} | {file_path}\n{snippet}" for ...]
scores = reranker.predict(query, texts)
```

### Рекомендовані покращення input

1. **Додати function/class names** (з `chunk_type=code_function` chunks):
   ```
   backoffice-web | src/Components/PreviewModal/PreviewModal.tsx
   Functions: PreviewModal, openPreview, closePreview
   Imports: PreviewFile, DocumentUploadStatus
   ---
   [snippet]
   ```

2. **Додати repo context**:
   ```
   Repo: backoffice-web (frontend React app for operations)
   File: src/Components/PreviewModal/PreviewModal.tsx
   ---
   [snippet]
   ```

3. **Виділити query intent**:
   - Якщо query містить UI-ключові слова → додати тег `[UI_COMPONENT]`
   - Якщо query містить backend-ключові слова → додати тег `[BACKEND_LOGIC]`
   - Reranker може використовувати ці теги для кращого ранжування

---

## 9. Метрики успіху

| Етап | Цільова метрика | Мінімальний success |
|------|-----------------|---------------------|
| Дообучення embeddings | recall@10 на retrieval-only (без reranker) | +5pp |
| Дообучення reranker'а | recall@10 на full pipeline | +3pp |
| Full pipeline | hit@10 / recall@10 | hit@10 ≥ 70%, recall@10 ≥ 20% |

---

## 10. Наступні кроки (по пріоритету)

1. **Запустити повний eval на 665 запитах** — отримати чесний recall@10 baseline (~40 хв)
2. **Зібрати near-miss dataset** (~200 запитів, де expected файл у пулі, але не в топ-10)
3. **Ручна анотація 50 найскладніших запитів** — перевірити expected файли
4. **Навчити reranker на near-miss pairs** — hard negatives з топ-10
5. **Оцінити impact** — повторний eval
6. **Якщо reranker не дає +3pp → дообучити embeddings**

---

## Додаток: Список 10 найгірших запитів (offset 200) для першої ітерації training data

1. Handle Expired Cloudflare Sessions Gracefully
2. Remove Merchant Include/Exclude for Internal Rules
3. CRM Fields Refactor for Entity and Merchant Support
4. Fix Multi-Page Document Preview in Backoffice
5. Extend VAT Number Validation with Legal Name and Address Comparison
6. Audit All GraphQL Queries in Backoffice
7. Clean Up GraphQL Schema and Authorization After Query Audit
8. Create Backoffice Service for Managing Group-to-Permission Mapping
9. Refactor VAT Number Handling in PayPass and Backoffice
10. Improve VAT Number Input Guidance and Validation in PayPass

Для кожного з цих запитів expected файли НЕМАЄ в топ-10 через semantic mismatch (retrieval failure) або reranker mis-ranking.
