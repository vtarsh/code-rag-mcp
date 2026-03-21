# Pay-Knowledge MCP Benchmark & Improvement Roadmap

## Current Status
- **Phase 0: COMPLETE** (2026-03-21) — baseline зібраний, аудит пройдений
- **Phase 0.5: COMPLETE** (2026-03-21) — ground truth verified for Q0-Q5, stored in GROUND-TRUTH.md
- **Phase 1: COMPLETE** (2026-03-21) — all 4 fixes done and tested
- **Phase 2: COMPLETE** (2026-03-21) — re-test shows significant improvement
- **Phase 3: COMPLETE** (2026-03-21) — proactivity test: 83% TP, 0% FP WITHOUT gotchas
- **Phase 4: COMPLETE** (2026-03-21) — 8 diagnostic templates indexed as reference docs
- **Phase 5: COMPLETE** (2026-03-21) — 3/39 gotchas covered by code_facts, 36 still needed
- **Phase 6: COMPLETE** (2026-03-21) — regression suite: conceptual 0.85, realworld 0.80, flows 0.71
- **Next action:** Phase 7 (foundation cleanup) → Phase 8 (benchmark expansion) → Phase 9 (MOONSHOT: Failure Propagation Engine)

## Vision: Failure Propagation Engine (THE MOONSHOT)

**Goal:** Not "where is this code" → but "what happens when this code breaks"
**Why unique:** Nobody has built a causal model of microservice failure propagation tied to code intelligence.

### Layer 2 (FIRST — most structured, highest ROI):
- **State Machine Extraction** — payment status transitions per provider
- Extract from: status enums, switch statements, workflow activity chains
- Demo query: "trace full lifecycle of Trustly DirectDebit: every status, retry point, failure branch"

### Layer 1 (SECOND):
- **Error Handling Semantics** — try-catch → graph edges showing failure paths
- "Service A calls B → if B fails → A retries 3x → then falls back to C → if C fails → payment stuck in PENDING"

### Layer 3 (DEFERRED):
- **Temporal Reasoning** — timing dependencies, cron, settlement windows, retry backoffs

### Layer 4 (DEFERRED):
- **Counter-factual Queries** — "what if Trustly goes down for 30 min? Which payments stuck? Recovery path?"

## Phase 7a: Provider Documentation Scraping (next session or parallel)

**Goal:** Стягнути ВСЮ публічну документацію кожного провайдера і зберегти в knowledges.

### Що стягувати:
- API reference: всі endpoints, request/response schemas, всі типи відповідей
- **Описи і примітки** — не тільки API specs, а й текстові пояснення, caveats, limitations
- Error codes та їх значення
- Webhooks documentation (event types, payloads, retry policies)
- Sandbox/test credentials інструкції
- **Все** — кожне посилання повноцінно, без скорочень

### Як стягувати:
- **Tavily API** (є ключі) — найшвидший варіант, передаєш URL + параметри, отримуєш structured content
- **Browser UI extension (Claude in Chrome)** — для сайтів що блокують bots або потребують JS rendering
- **WebFetch** — для простих URL що віддають HTML
- Fallback: manual copy якщо нічого не працює

### КРИТИЧНЕ правило ізоляції:
- **Кожен агент = свій окремий tab** в browser UI
- НІКОЛИ два паралельних агенти не використовують один tab
- Перед запуском агента — `tabs_create_mcp` для нового tab
- Після завершення — `tabs_close_mcp` для cleanup

### Провайдери для scraping (priority):
1. Trustly — verification, DirectDebitMandate, webhooks
2. Worldpay — payouts, retry policies, XML API
3. Stripe — reference implementation, best-in-class docs
4. PayPal — APM flow, redirect handling
5. Nuvei — card + APM hybrid
6. Всі інші по черзі

### Storage:
- Зберігати в `profiles/my-org/docs/providers/<provider-name>/`
- Індексувати як `file_type: provider_docs`
- Кожен provider = окрема директорія з markdown файлами
- Зв'язати з відповідним grpc-apm-* або grpc-providers-* repo в графі

## Phase 7: Foundation Cleanup (next session)
- Fix LIMIT 1 bug in diff_provider_config (multi-PMT providers)
- Add 3 regex: process.env with defaults, Temporal retry policies, gRPC status mapping
- Expand code_facts to ~140 meaningful repos (filter boilerplate/stubs)
- Template literal throw support in regex
- .gitignore: add *.pem, *.key, .env*, credentials*, *secret*

## Phase 8: Benchmark Expansion
- 30-50 out-of-distribution questions from real Slack/Jira
- 10 runs per question for statistical validity
- Retrieval-only eval (isolate indexer from LLM quality)
- Independent scoring (blind evaluation)

## Phase 9: Failure Propagation Engine (months)
- Start with Layer 2 (state machines) — most structured, highest signal-to-noise
- Then Layer 1 (error handling semantics)
- Prove value before expanding to Layers 3-4

## Analysis Summary (9 agents, 2026-03-21)
- 6 analysis agents: edge cases, JS patterns, meta-critique, best practices, priority, security
- 2 critics: technical (deflated numbers, challenged tree-sitter) + strategy (moonshot vision)
- 1 fresh reviewer: hybrid A+B, state machines first, "library → advisor" gap
- Key insight: dependency graph = unique moat, but topology without SEMANTICS is replicable
- Security audit: SAFE, minor .gitignore additions needed

### Implemented Fixes (Phase 1)
- **Fix 1 (seeds.cql):** DONE — all boolean values now explicit (enabled/disabled/feature flags matrix)
- **Fix 2+3 (code_facts + const values):** DONE — 598 facts extracted from 136 repos (403 validation_guards, 186 const_values, 9 joi_schemas)
- **Fix 4 (diff_provider_config):** PENDING

### Backups
- iCloud: `~/Library/Mobile Documents/com~apple~CloudDocs/pay-knowledge-profile/`
  - `backups/knowledge-20260321-with-code-facts.db` — DB with code_facts
  - `BENCHMARK-ROADMAP.md`, `GROUND-TRUTH.md` — docs
  - Profile configs (glossary, flows, etc.) — existing

## Мета
Побудувати превентивну систему пошуку по кодовій базі (500+ JS мікросервісів), яка знаходить відповіді з першоджерел (код, конфіги, proto), а не з ручних підказок (gotchas).

## Філософія
- **НЕ латати дірки gotchas** — покращувати індексатор
- **Gotchas = fallback** для речей які принципово неможливо витягти з коду
- **Baseline ПЕРЕД фіксами** — щоб було з чим порівнювати
- **Кожен крок = аудит** незалежними агентами перед закриттям
- **Ground truth ПЕРЕД scoring** — спочатку документуємо правильну відповідь, потім оцінюємо

## Codebase Context (для імплементаторів)

### JS без типів
- Кодова база = 500+ JS мікросервісів, **немає TypeScript**
- Proto files (.proto) = type system замість TS types
- ts-morph НЕ підходить → tree-sitter для AST parsing
- Runtime signals (require, process.env, CQL queries) надійніші за static analysis

### Validation/typing layer
- **Joi** — поточний runtime validation (compensating for no TS). Шукати: `.validate()`, `Joi.object({...})`
- **Zod** — міграція в процесі. Шукати: `z.object({...})`, `.parse()`
- code_facts extraction повинен покривати ОБИДВА

### @pay-com/* internal libraries
- ВСІ org-scoped npm packages — це репо в pay-com org, клонуються і перевіряються
- Validation logic може жити в shared libs (node-libs-common, node-libs-types тощо), НЕ тільки в сервісах
- code_facts extraction повинен слідувати за `require('@pay-com/...')` chains

### Proto architecture
- `providers-proto` — основний proto контракт (ProviderService з 8 RPC)
- **Common types** — в ОКРЕМОМУ репо (types), import-яться через proto
- **Proto2 → Proto3 migration** — ongoing. Proto2 має `required` поля, proto3 — ні
- Impact analysis повинен враховувати обидва репо

### E2E testing (CORRECTED — не "manual only")
- E2E код деплоїться на dev environment
- Тригериться **з локальної машини під VPN** (не manual clicking!)
- Скрипти для e2e пишуться розробником
- Generic e2e flow = додати provider name + type (apm/card) після деплою
- CI тригерить через `workflow_dispatch` → `e2e-tests` repo

## Ground Truth Corrections

| Питання | Стара відповідь (невірна) | Правильна відповідь |
|---------|--------------------------|---------------------|
| Q5 E2E | ~~"APM = manual only"~~ Initially thought wrong, but actually CORRECT in code | APM e2e scripts are gitignored, per-developer (e.g. grpc-apm-trustly/scripts/), NOT in any repo. This is tribal knowledge. Generic card e2e = automated in CI. APM = developer writes scripts locally, tests via VPN, links transactions from backoffice to Jira. |

## Фаза 0: Baseline (ЗАВЕРШЕНА, потребує доповнення)

### Методологія
- 6 питань (Q0-Q5) по 5 типах: config, behavioral, impact, process, e2e
- 3 групи агентів: A (grep), B (MCP без gotchas), C (MCP повний)
- Технічний фільтр `exclude_file_types=gotchas` замість behavioral "ignore"
- Метрики: recall, precision, false positives, consistency

### Результати baseline

| Питання | Тип | Grep | MCP no-gotchas | Висновок |
|---------|-----|------|----------------|----------|
| Q0 PI-54 | config | 3.5/6 | 4/6 (з gotchas знайшов, без — 2-4/6) | MCP inconsistent без gotchas |
| Q1 EPX features | config | 2/2 | 2/2 (1 run 1.5/2 — галюцинація) | ~Рівні, MCP truncation gap |
| Q2 Worldpay retry | behavioral | Повні значення констант | Структура без значень | **Grep виграє** — MCP не індексує const values |
| Q3 Proto deps | impact | 65 repos, 34 initialize | 60 repos, 29 initialize | **Grep повніший**, MCP швидший |
| Q4 Add RPC method | process | 10+ файлів з line numbers | 6 repos з файлами | **~Рівні**, різні переваги |
| Q5 E2E testing | e2e | Повний CI chain | Частковий | **Grep виграє** — MCP docs outdated |

### Виявлені gaps MCP

1. **Seeds.cql truncation** — рядки обрізаються, агент не бачить повні значення
2. **Const values не індексуються** — MAX_RETRIES=5, N_RETRIES_WITHOUT_TIMEOUT=2 невидимі
3. **If-conditions не індексуються** — `if (!mit) throw` невидимий
4. **Inconsistency** — один і той же запит дає 2/6 або 4/6 в різних runs
5. **Outdated docs/flows** — APM e2e описаний як "manual only", хоча це неправда
6. **Неповний список repos** — MCP знайшов 60 замість 65 (пропустив 5 APM repos)

### Audit findings (Phase 0)
1. **FAIL: Statistical validity** — потрібні 5 runs per agent-question (зараз 1-3)
2. **NEEDS-WORK: Ground truth** — Q5 обидва агенти дали неправильну відповідь; потрібен verified GT doc
3. **NEEDS-WORK: Scoring** — різні denominators (X/6 vs X/2 vs qualitative); потрібен binary rubric
4. **NEEDS-WORK: Hallucination tracking** — Q1 B2 галюцинував "payout in some configs"; треба рахувати
5. **NEEDS-WORK: Missing question types** — немає cross-service runtime flow, error handling, Temporal, shared lib questions

## Фаза 0.5: Ground Truth + Statistical Completion (НАСТУПНА)

### Що робити:
1. Для кожного Q0-Q5 створити verified ground truth document:
   - Правильна відповідь
   - Точні file paths і line numbers
   - Чи відповідь повністю derivable з коду (чи потрібне tribal knowledge)
2. Довести кожну пару agent-question до 5 runs
3. Визначити scoring rubric per question з binary reference points:
   - Factual correctness (кожен fact = 1 point, found/not-found)
   - Hallucination count (окремо від missing facts)
   - Specificity (file paths, line numbers — bonus, не штраф)
4. Report: mean + variance per agent per question

### Definition of Done Phase 0.5:
- GT doc для всіх 6 питань, verified grep-ом
- 5 runs кожен, variance <1.5x
- Scoring rubric applied consistently

## Фаза 1: Покращення індексатора (IN PROGRESS)

### Fix 1: Повне seeds.cql індексування
- **Проблема:** Рядки truncated після ~300 chars
- **Рішення:** Парсити INSERT statements, індексувати кожну колонку окремо
- **Нова таблиця:** `provider_configs` з колонками provider, payment_method_type, кожен feature flag
- **Effort:** 2-3 дні
- **Тестується:** Q0 (payment_method_type), Q1 (EPX features)

### Fix 2: Code facts extraction (validation guards)
- **Проблема:** MCP не бачить `if (!mit) throw "Only MIT allowed"`
- **Рішення:** Regex або tree-sitter для витягу patterns
- **Extraction targets:**
  - `if (condition) throw/return error` — validation guards
  - `Joi.object({...})` / `.validate()` — joi schemas
  - `z.object({...})` / `.parse()` — zod schemas
  - `require('@pay-com/...')` chains — flag that validation may live in imported libs
- **Нова таблиця:** `code_facts` (repo, file, function, condition, message, line)
- **Effort:** 3-5 днів (regex), 2-3 тижні (tree-sitter)
- **Тестується:** Q0 (MIT requirement), Q2 (RETRYABLE_ERRORS values)
- **ВАЖЛИВО:** Сканувати не тільки service repos, а й @pay-com/* shared libs

### Fix 3: Const values indexing
- **Проблема:** MCP знає що `MAX_RETRIES` існує, але не знає що = 5
- **Рішення:** Regex для `const X = value` та `module.exports = { X: value }`
- **Extends:** code_facts table
- **Effort:** 1-2 дні
- **Тестується:** Q2 (retry constants)

### Fix 4: diff_provider_config tool
- **Проблема:** Немає способу порівняти конфігурацію двох провайдерів
- **Рішення:** Новий MCP tool який порівнює seeds.cql записи
- **Effort:** 1 тиждень
- **Тестується:** Q0 (trustly vs інші)

## Фаза 2: Re-test (після Фази 1)

- Ті ж 6 питань, ті ж 3 групи агентів, 5 runs кожен
- Порівняння з baseline: чи покращилась recall, precision, consistency
- Якщо покращення <20% — аналізувати чому і повертатись до Фази 1

## Фаза 3: Proactivity test

- Реконструювати стан коду ДО PI-54 бага
- Дати агенту задачу: "Review Trustly integration for completeness"
- Перевірити чи він САМА знайде проблеми
- Target: ≥60% true positive, <30% false positive

## Фаза 4: Diagnostic trace templates

- Для топ-10 error classes написати структуровані діагностичні шляхи
- "Для provider payment failure → перевір: seeds.cql → feature flags → proto"
- Зберігати як searchable documents в pay-knowledge

## Фаза 5: Архівування gotchas

- Для кожної gotcha перевірити: чи покривається тепер індексатором?
- Якщо так → архівувати (не видаляти, перемістити в archive/)
- Якщо ні → залишити як fallback
- **Definition of Done:** Метрики Phase 2 не регресують після архівування; gotchas archive має changelog

## Фаза 6: Regression Suite

- Конвертувати всі Q0-Q5 + нові питання в автоматизований regression suite
- Мінімальні acceptable scores per question type
- Інтегрувати в CI приватного репо: будь-яка зміна індексатора повинна пройти suite
- Документувати "gotcha-free" baseline

## Інфраструктурні рішення

### Profile backup
- Приватний репо `vtarsh/pay-knowledge-profile` для backup профілю
- Symlink `~/.pay-knowledge/profiles/my-org/ → ~/repos/pay-knowledge-profile/`
- **Security gate (перед першим push):**
  1. 3 незалежних агенти сканують profile на secrets (*.pem, *.key, .env, API keys, tokens)
  2. .gitignore повинен містити: `*.pem`, `*.key`, `.env*`, `credentials*`, `*secret*`
  3. Periodic re-scan при кожному значному оновленні профілю

### Project-specific vs Generic
- Generic MCP → публічний репо (pay-knowledge)
- Project-specific (profiles, rules, configs) → приватний репо (vtarsh)
- @pay-com/* libraries → клонуються і перевіряються при індексації

### JS-specific challenges
- ts-morph НЕ підходить для JS без типів → tree-sitter
- Proto files = type system (замість TS types)
- Runtime signals (require, process.env, CQL queries) > static analysis
- Function-level chunking для embeddings (50-200 lines = 200-800 tokens)

## Phase 2: Re-test Results

| Metric | Baseline | After Phase 1 | Target | Status |
|--------|----------|---------------|--------|--------|
| Q0 PI-54 recall | 2-4/6 | **5/6** | ≥5/6 | ✅ MET |
| Q2 const values found | 0/3 | **3/3** | ≥80% | ✅ MET |
| Q1 hallucinations | 1 false positive | **0** | 0 | ✅ MET |
| Q2 total score | 5/10 | **8/10** | ≥7/10 | ✅ MET |
| Q3 graph completeness | 60/63 repos | 60/63 | ≥95% | ⚠️ 95.2% |
| Proactivity true positive | N/A | N/A | ≥60% | 🔲 Phase 3 |
| Proactivity false positive | N/A | N/A | <30% | 🔲 Phase 3 |

## Ключові метрики (targets)

| Метрика | Target |
|---------|--------|
| Proactivity true positive | ≥60% |
| Proactivity false positive | <30% |
| Regression suite pass rate | 100% after changes |
