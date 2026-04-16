# MCP Improvement: Vault Service Discovery

## Проблема

При дослідженні нового payment method type (eu_bank_account SEPA payouts, PI-61) ми пропустили критичний сервіс `grpc-vault-bankaccounts`. Це призвело до того що ми проектували власне рішення для зберігання provider-specific token (userPaymentOptionId), коли vault вже мав механізм `processorExistingTokenIds`.

**Чому пропустили:**
- `mcp__code-rag-mcp__analyze_task` не показав `grpc-vault-bankaccounts` в core/related repos
- Агенти згадували vault в контексті Volt provider, але не виділили його як окремий шар
- Investigation framework не має чекпойнту для vault/data-store layer

**Наслідки:** ~4 години дослідження в неправильному напрямку, хибний дизайн source structure.

## Що потрібно зробити

### 1. MCP repo зв'язки (analyze_task)

Коли задача стосується `bank_account`, `eu_bank_account`, `us_bank_account`, `in_bank_account`, payout, або payment method — `grpc-vault-bankaccounts` має з'являтись в core/related repos.

Аналогічно `grpc-vault-cvv` для card типів.

Дослідити:
- Як `analyze_task` визначає core/related repos
- Де конфігурація зв'язків між repos і keywords
- Додати зв'язок: `bank_account*` keywords → `grpc-vault-bankaccounts`
- Додати зв'язок: `vault`, `tokenize`, `sensitive data` → відповідні vault сервіси

### 2. Investigation framework (gotchas)

Додати в Stage 3 ("Check each hop") або Stage 6 ("Verify end-to-end") чекпойнт:

```
## Data store hops (часто пропускаються)

Payment method data проходить через vault ПЕРЕД провайдером:
- Card data → grpc-vault-cvv (token → card number/cvv)
- Bank account data → grpc-vault-bankaccounts (token → IBAN/account number)
  - Має поле `processorExistingTokenIds` — зберігає provider-specific token (e.g. Nuvei UPO ID)

При дослідженні нового payment method type:
1. Визначити який vault service обслуговує цей тип
2. Прочитати proto vault service — які поля доступні
3. Подивитись як reference provider (volt, paysafe) використовує vault для цього типу
```

**Критерій:** чи вирішить це проблему? Так — наступна сесія при дослідженні bank_account payout побачить `grpc-vault-bankaccounts` в analyze_task і чекпойнт в investigation framework. Обидва шляхи ведуть до vault.

**Громіздкість:** мінімальна — один зв'язок в MCP конфігурації + 5-7 рядків в gotchas.
