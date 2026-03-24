# /scrape-docs — Provider Documentation Scraper

Fetch and index provider API documentation for use in audits and analysis.

## Usage
```
/scrape-docs trustly https://docs.trustly.com     # Open docs via Tavily
/scrape-docs payper --browser                       # Closed docs via browser extension
/scrape-docs --list                                 # Show existing provider docs
```

## Option A: Open Documentation (Tavily Crawler)

For publicly accessible API docs:
```bash
cd ~/.pay-knowledge && python3 profiles/pay-com/scripts/tavily-docs-crawler.py \
  <DOCS_URL> <provider_name> \
  --limit 100 --max-depth 3
```

Output: `~/.pay-knowledge/.secrets/provider-docs/{provider}/`
Then copy to: `profiles/pay-com/docs/providers/{provider}/`

Tavily API key: `~/.pay-knowledge/.secrets/tavily-keys.json` or `TAVILY_API_KEY` env var.

## Option B: Closed Documentation (Browser Extension)

For docs behind auth (login required, Notion, Confluence):

1. Ask user to provide access via browser
2. User connects via browser extension and navigates to docs root page
3. Launch **one sequential agent** (Playwright MCP shares single browser — parallel agents conflict)
4. **Follow ALL internal links** — cross-reference links are often the most important pages
5. For each page, scrape EVERYTHING:
   - Full page content (text, code samples, tables)
   - Response examples: look for 200, 400, 401, 403, 404, 500 status codes
   - Click "Try it" / "Test" / "Send" buttons to reveal real responses
   - Click on each status code tab (200, 4xx, 5xx)
   - Expand all collapsed sections — error codes, enums, field descriptions
   - Request/response schemas with all fields and types
6. Save each page as markdown to `profiles/pay-com/docs/providers/{provider}/`
7. Scrape ALL pages — don't skip anything

## After Fetching Docs

```bash
# Index for search
cd ~/.pay-knowledge && python3 scripts/build_index.py
# Or incremental: python3 scripts/build_vectors.py --repos=provider-docs
```

## Existing Provider Docs (27 providers)

```
profiles/pay-com/docs/providers/
aps, braintree, checkout, credorax, ecp, flutterwave, gumballpay,
monek, neosurf, nexi, nuvei, paylands, paynearme, paypal, payper,
paysafe, plaid, ppro, rapyd, silverflow, stripe, stripe-cashapp,
tabapay-crawl, tabapay-extract, trustly, volt, worldpay
```

Missing: okto, iris, ilixium
