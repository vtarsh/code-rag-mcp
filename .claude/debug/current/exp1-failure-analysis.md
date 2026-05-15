# Session2 failure analysis — n=663 clean eval misses

Bench: `bench_runs/jira_clean_n663_baseline_session2.json` (FTS5+expand_query-OFF+F-bucket-fix).
Metric: 441/663 hits (66.5%), **222 misses (33.5%)**.

## Top-1 file_type on misses — FRONTEND DOMINATES

| file_type on top-1 | misses | % of misses |
|---|---|---|
| **frontend** | **105** | **47.3%** |
| library | 34 | 15.3% |
| route | 14 | 6.3% |
| provider_doc | 12 | 5.4% |
| grpc_method | 12 | 5.4% |
| env | 10 | 4.5% |
| reference / config / workflow / ci / other | 38 | 17.1% |

47% of misses surface a frontend file at top-1 instead of the right code. Either
- frontend chunks dominate the FTS+vector pool for short PR-title queries,
- or the right code IS in pool but reranker pushes frontend up.

## Top GT repos on misses — BACKOFFICE-WEB IS THE FAILURE EPICENTER

| repo | GT pairs in misses |
|---|---|
| **backoffice-web** | **962** |
| hosted-fields | 228 |
| graphql | 217 |
| express-api-v1 | 146 |
| grpc-payment-gateway | 99 |
| grpc-core-schemas | 91 |
| workflow-tasks | 55 |
| workflow-provider-webhooks | 47 |
| grpc-core-tasks | 47 |
| node-libs-common | 46 |

`backoffice-web` is a frontend repo. 962 GT pairs on missed queries tracks with
the 47% frontend file_type — failures concentrate where frontend is.

## Implications for STAGE 3 (DE2 + RE3 bundle, queued for next session)

1. **Extractor relax priority**: focus on `backoffice-web` frontend coverage. Per index-gap-detective B/E bucket: frontend has chunks but many `src/Pages/` and `src/Components/` files filtered. Relaxing FE allowlist would directly reduce these misses.
2. **RE3 code-aware tokenizer**: `backoffice-web` files have heavy camelCase paths (`MerchantSettlementAccount.tsx`, `getSettlementAccounts.ts`). Code-aware tokenizer + path-as-doc column likely lifts these.
3. **Frontend-specific reranker bias**: separate concern — even if file is in pool, reranker may not know it's the target. RE1 (repo prefilter) was tested and NOISE — the broader signal (file_type penalty/boost specific to FE) would need separate tuning.
4. **Estimated lift potential**: if extractor + tokenizer changes recover ~50% of the 105 frontend misses, that's +50 hits / 663 = **+7.5pp** on jira_clean hit@10.

## Other failure-pattern observations

- Query length isn't differential (mean 6.0 misses vs 6.3 hits, median 6/6) — short PR titles are not specifically broken.
- `library` file_type (34 misses) suggests some misses are right-domain-wrong-file-type (PR touched a generic library, but reranker chose a specific one).
- `provider_doc` (12) + `reference` (9) = 21 misses where doc dominates; expand_query-OFF should have helped here, but residual noise remains. Could be additional B/P asymmetry not captured by W1.
