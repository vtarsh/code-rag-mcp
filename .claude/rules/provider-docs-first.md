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
2. User connects via browser extension and navigates to docs root page
3. Launch **background agents** to scrape — one agent per browser tab, STRICTLY:
   - **ONE tab per agent** — agents MUST NOT share tabs or they overwrite each other
   - Each agent scrapes ONE page/section completely before moving to next
   - Large docs = many pages = takes time. This is expected.
4. For each page, scrape EVERYTHING:
   - Full page content (text, code samples, tables)
   - **Response examples**: look for 200, 400, 401, 403, 404, 500 status codes
   - **If responses not visible**: click "Try it" / "Try me" / "Test" / "Send" buttons to reveal real responses
   - **Click on each status code tab** (200, 4xx, 5xx) to see actual response bodies
   - **Expand all collapsed sections** — error codes, enums, field descriptions
   - Request/response schemas with all fields and types
5. Save each page as markdown to `profiles/pay-com/docs/providers/{provider}/`
6. Scrape ALL pages — don't skip anything. Full API reference, guides, webhooks, error codes, authentication.

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
