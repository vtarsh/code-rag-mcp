# Provider Documentation — Always Fetch First

## Rule: Never Assume API Formats

Before making ANY claim about a provider's API response format, error structure, status codes, or capabilities:

1. **Check if docs already exist**: `ls profiles/pay-com/docs/providers/{provider}/`
2. **If docs exist** → read them before auditing code
3. **If docs DON'T exist** → ask user for documentation URL before proceeding

## How to Fetch Provider Docs

### Option A: Open documentation (Tavily crawler)
For publicly accessible API docs:

```bash
cd ~/.pay-knowledge && python3 profiles/pay-com/scripts/tavily-docs-crawler.py \
  <DOCS_URL> <provider_name> \
  --limit 100 --max-depth 3
```

Examples:
```bash
python3 profiles/pay-com/scripts/tavily-docs-crawler.py https://developerhub.ppro.com ppro
python3 profiles/pay-com/scripts/tavily-docs-crawler.py https://developer.paysafe.com paysafe --limit 100
```

Output: `~/.pay-knowledge/.secrets/provider-docs/{provider}/` → then copy to `profiles/pay-com/docs/providers/{provider}/`

Tavily API key: `~/.pay-knowledge/.secrets/tavily-keys.json` or `TAVILY_API_KEY` env var.

### Option B: Closed documentation (browser extension)
For docs behind auth (login required, Notion, Confluence, etc.):

1. Ask user to provide access via browser
2. User connects via browser extension and navigates to docs
3. Scrape available pages through the UI
4. Save to `profiles/pay-com/docs/providers/{provider}/`

### After fetching docs:
1. Index them: `python3 scripts/build_index.py` (or incremental via `build_vectors.py --repos=provider-docs`)
2. They become searchable via MCP RAG tools
3. Use them in audits, task analysis, and implementation guidance

## When to Apply

- **Before ANY impact audit** of a provider task (PI-*)
- **Before suggesting implementation patterns** (polling vs webhook, redirect vs direct)
- **Before claiming error format/status codes** are wrong or missing
- **When user asks about a new provider integration**

## Why This Matters (PI-60 Lesson)

Audit claimed `message?.[0]?.error` was dead code. Reality: Payper returns `message` as array of objects `[{"error": "quantity should be integer!", "item_index": 0}]`. The audit was WRONG because it assumed the format without checking docs or sandbox. This kind of mistake erodes trust in the RAG system.

## Existing Provider Docs (14 providers)

```
profiles/pay-com/docs/providers/
├── braintree/     ├── paylands/      ├── silverflow/
├── checkout/      ├── paynearme/     ├── stripe-cashapp/
├── nuvei/         ├── paysafe/       ├── tabapay-crawl/
├── plaid/         ├── ppro/          ├── tabapay-extract/
├── volt/          └── worldpay/
```

Missing docs for active providers: trustly, rapyd, okto, iris, aps, gumballpay, neosurf, payper, ilixium, nexi
