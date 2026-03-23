# Overnight State Tracker

## Started: 2026-03-24 night

## CORE Tier 1 Queue (11 tasks)
- [x] CORE-1597 (12) — 100% recall. sync→async issuerResponseCode.
- [x] CORE-2620 (9) — 100% recall. Risk rules levels. 3 phantoms.
- [x] CORE-2552 (9) — 100% recall. Settlement disabling.
- [x] CORE-2606 (9) — 100% recall. Descriptor suffix.
- [x] CORE-2551 (9) — 88.9% recall. grpc-risk-logs = pkg bump.
- [x] CORE-2602 (8) — 100% recall. Stripe 3DS per-brand creds.
- [x] CORE-2488 (8) — 100% recall. Retries with settlement accounts.
- [x] CORE-2545 (7) — 100% recall. 3DS fingerprint linked auth.
- [x] CORE-2580 (7) — 100% recall. PMO config set for bank transfers.
- [ ] CORE-2582 (7) — IN PROGRESS (batch 4)
- [ ] CORE-2203 (7) — IN PROGRESS (batch 4)

## BO Tier 1 Queue (top 15)
- [ ] BO-953 (13) — IN PROGRESS (batch 4)
- [ ] BO-1485 (9) — batch 5
- [ ] BO-1332 (8) — batch 5
- [ ] BO-1479 (6) — batch 5
- [ ] BO-708 (6) — batch 6
- [ ] BO-1344 (6) — batch 6
- [ ] BO-1283 (6) — batch 6
- [ ] BO-1280 (6) — batch 7
- [ ] BO-934 (6) — batch 7
- [ ] BO-1345 (6) — batch 7
- [ ] BO-1160 (6) — batch 8
- [ ] BO-1580 (5) — batch 8
- [ ] BO-954 (5) — batch 8
- [ ] BO-1279 (5) — batch 9
- [ ] BO-1065 (5) — batch 9

## Progress
- Batches 1-3: 9 CORE DONE (8x 100%, 1x 88.9%)
- Batch 4: CORE-2582 100%, CORE-2203 85.7%, BO-953 100% — DONE
- Batch 5: BO-1485 88.9%, BO-1332 100% + pattern mining — DONE
- Batch 6: BO-1479 80%, BO-708 100%, BO-1344 100% — DONE
- Batch 7: BO-1283 100%, BO-1280 100%, BO-934 66.7% — DONE
- Batch 8: BO-1345, BO-1160, BO-1580 — launching
- Tasks completed: 20/26
- Pattern mining: round 1 done, round 2 due at 24 tasks
- Commits: 1 (f533e29)

## Baseline
- TOTAL: 96.8% recall, 1.0% precision (1011/1044 found)

## Pattern Mining Round 1 (after 12 tasks)
- Added 6 new co-change rules to conventions.yaml
- Added grpc-core-schemas → kafka-cdc-sink rule
- Result: 96.8% → 96.9% (+1 CORE, +1 BO repo recovered)
- Identified 7 actionable items (see pattern mining agent output)
- Top remaining: pkg-bump filtering, hub penalty, graphql edge gaps

## Key Finding: pkg: Virtual Node Dead-End (batches 6-7)
- `build_graph.py` creates `pkg:@pay-com/X` virtual nodes but NEVER resolves them to actual repos
- graphql→pkg:@pay-com/risk-alerts is a dead end — no edge to grpc-risk-alerts
- Same for graphql→pkg:@pay-com/core-tasks, pkg:@pay-com/core-configurations, etc.
- Affects: BO-934 (2 misses), BO-1479 (1 miss), and likely many more BO tasks
- **#1 priority fix**: resolve pkg:@pay-com/X → grpc-X or X repo in build_graph.py
- Also: graphql has only 3 grpc_client_usage edges but should have 15+
- grpc-auth-permissions missed in 2 tasks (BO-1332, BO-1479) — new service companion pattern

## Key Patterns (batches 1-3)
1. **Package-bump-only repos** are the ONLY source of tool misses (grpc-risk-logs, grpc-core-reconciliation, grpc-core-paymentlinks)
2. **Precision crisis**: 1.6-7.5% across all tasks. Hub cascade via libs-types (423 deps) dominates.
3. **100% independent grep recall** on 8/9 tasks — keyword signal is strong for well-described CORE tasks.
4. **grpc-core-reconciliation** appears as pkg-bump in 3 different tasks.
5. **CORE-2580 insight**: grpc-core-schemas has the actual code (pmcf_ prefix) but was changed in companion task CORE-2606 instead.
