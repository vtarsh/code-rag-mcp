# EXP1 — clean jira eval stats

- Input: `profiles/pay-com/eval/jira_eval_n900.jsonl`
- Output: `profiles/pay-com/eval/jira_eval_clean.jsonl`
- DB: `db/knowledge.db` (29,691 distinct (repo, file) pairs)
- min_gt threshold: 3

## Counts

- original n_queries = 910
- original total GT pairs = 22,520
- dropped noise pairs = 5,518
- dropped unhittable pairs = 6,081
- suffix-matched (F-bucket recovered) = 1,450
- queries dropped (GT < 3) = 245 (26.92%)
- final n_queries = 665
- final total GT pairs = 10,650
- mean GT/query: before = 24.75, after = 16.02
- total GT pairs dropped = 52.71% of original

## Top 20 repos in cleaned set (by GT-pair count)

| rank | repo | gt_pairs |
|---|---|---|
| 1 | backoffice-web | 3339 |
| 2 | graphql | 1033 |
| 3 | express-api-v1 | 437 |
| 4 | hosted-fields | 297 |
| 5 | grpc-payment-gateway | 250 |
| 6 | workflow-tasks | 224 |
| 7 | workflow-provider-webhooks | 222 |
| 8 | grpc-core-schemas | 204 |
| 9 | node-libs-common | 187 |
| 10 | libs-types | 148 |
| 11 | workflow-provider-onboarding-webhooks | 143 |
| 12 | workflow-onboarding-merchant-providers | 115 |
| 13 | grpc-core-tasks | 115 |
| 14 | grpc-providers-credentials | 108 |
| 15 | paypass-web | 106 |
| 16 | grpc-risk-rules | 99 |
| 17 | express-api-internal | 83 |
| 18 | grpc-providers-silverflow | 73 |
| 19 | grpc-core-disputes | 69 |
| 20 | grpc-onboarding-entity | 60 |
