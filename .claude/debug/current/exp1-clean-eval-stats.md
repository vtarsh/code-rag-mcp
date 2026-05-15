# EXP1 — clean jira eval stats

- Input: `profiles/pay-com/jira_eval_n900.jsonl`
- Output: `profiles/pay-com/jira_eval_clean.jsonl`
- DB: `db/knowledge.db` (29,682 distinct (repo, file) pairs)
- min_gt threshold: 3

## Counts

- original n_queries = 908
- original total GT pairs = 22,459
- dropped noise pairs = 5,506
- dropped unhittable pairs = 6,048
- suffix-matched (F-bucket recovered) = 1,447
- queries dropped (GT < 3) = 245 (26.98%)
- final n_queries = 663
- final total GT pairs = 10,634
- mean GT/query: before = 24.73, after = 16.04
- total GT pairs dropped = 52.65% of original

## Top 20 repos in cleaned set (by GT-pair count)

| rank | repo | gt_pairs |
|---|---|---|
| 1 | backoffice-web | 3339 |
| 2 | graphql | 1033 |
| 3 | express-api-v1 | 437 |
| 4 | hosted-fields | 297 |
| 5 | grpc-payment-gateway | 248 |
| 6 | workflow-tasks | 224 |
| 7 | workflow-provider-webhooks | 219 |
| 8 | grpc-core-schemas | 204 |
| 9 | node-libs-common | 182 |
| 10 | libs-types | 148 |
| 11 | workflow-provider-onboarding-webhooks | 143 |
| 12 | workflow-onboarding-merchant-providers | 115 |
| 13 | grpc-core-tasks | 115 |
| 14 | grpc-providers-credentials | 107 |
| 15 | paypass-web | 106 |
| 16 | grpc-risk-rules | 99 |
| 17 | express-api-internal | 83 |
| 18 | grpc-providers-silverflow | 73 |
| 19 | grpc-core-disputes | 69 |
| 20 | grpc-onboarding-entity | 60 |
