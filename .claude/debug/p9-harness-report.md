# P9 Harness Report — End-to-End Bench (router → docs-tower → reranker → top-10)

Date: 2026-04-25
Author: p9-harness-builder
Status: harness LANDED. baseline E2E numbers measured. no deploy decision yet.

## Goal

Architecture debate (P9 prep #1) flagged that prior bench
(`/tmp/bench_v3_n200_*.json`) used `rerank_off=True` — measured only the
bi-encoder R@10. The production pipeline is

    router (`_query_wants_docs`) → vector_search top-50 → reranker (CrossEncoder MiniLM-L-6-v2) → top-10

so the bi-encoder R@10 is the wrong layer for deploy decisions. This work adds
a `--rerank-on` mode that measures the FULL bi-encoder + reranker stack on the
same eval set with the same AND-gate logic.

## Files changed

- `scripts/benchmark_doc_intent.py` — new `--rerank-on` flag + `load_reranker()`
  + `rerank_candidates()` helpers + manifest fields (`rerank_on`,
  `rerank_model`, `retrieval_k`).
- `tests/test_benchmark_doc_intent.py` — new file. 8 tests covering the helper
  functions and CLI flag plumbing (no LanceDB / sentence_transformers fork).

Test count: 822 → 829 passing (+8). Suite green (1 unrelated skip).

## Baseline E2E run

Command:

    python3.12 scripts/benchmark_doc_intent.py \
      --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
      --model=docs --no-pre-flight \
      --rerank-on \
      --out=/tmp/bench_v3_n200_docs_e2e.json

Inputs: eval-v3 (n=192 scoreable rows, 0 gold), incumbent docs tower
`nomic-ai/nomic-embed-text-v1.5`, prod reranker
`cross-encoder/ms-marco-MiniLM-L-6-v2`, top-50 retrieval pool, top-10 final.

## Bench delta: rerank-off vs rerank-on (incumbent baseline)

| Metric          | rerank-off | rerank-on | delta    |
|-----------------|-----------:|----------:|---------:|
| recall@10       |    0.2289  |   0.2138  |  -0.0151 |
| ndcg@10         |    0.3506  |   0.3487  |  -0.0019 |
| hit@5           |    0.4115  |   0.4167  |  +0.0052 |
| hit@10          |    0.5365  |   0.5052  |  -0.0313 |
| latency p50 ms  |    110.03  |   385.46  |  +275.43 |
| latency p95 ms  |    173.63  |   709.98  |  +536.35 |

**Headline finding**: on the incumbent docs tower the production CrossEncoder
slightly *hurts* macro recall@10 (-1.5pp) and hit@10 (-3.1pp), while marginally
helping hit@5 (+0.5pp). It triples p50 latency (110 ms → 385 ms) and
quadruples p95 (174 ms → 710 ms).

This is the kind of finding the architecture debate predicted. The reranker
churns the top-10 in 156/192 = 81% of queries — most of those swaps are
neutral on the eval set (135 R@10-unchanged reorders), but on a non-trivial
slice (32 rows) the reranker actively pushes ground-truth files OUT of top-10.

## Per-stratum recall@10 deltas (rerank-on − rerank-off)

| stratum   |    off |     on |    delta | n  |
|-----------|-------:|-------:|---------:|----|
| aircash   | 0.3778 | 0.2907 |  -0.0871 |  9 |
| interac   | 0.4815 | 0.6296 |  +0.1481 |  9 |
| method    | 0.1833 | 0.1755 |  -0.0078 | 17 |
| nuvei     | 0.3978 | 0.2935 |  -0.1043 | 23 |
| payout    | 0.1008 | 0.0754 |  -0.0254 | 21 |
| provider  | 0.1913 | 0.2522 |  +0.0609 | 23 |
| refund    | 0.3987 | 0.3564 |  -0.0423 | 13 |
| tail      | 0.0950 | 0.1027 |  +0.0077 | 50 |
| trustly   | 0.2667 | 0.1833 |  -0.0834 |  4 |
| webhook   | 0.2797 | 0.2239 |  -0.0558 | 23 |

Reranker wins on `interac` (+14.8pp) and `provider` (+6.1pp); loses on
`nuvei` (-10.4pp), `aircash` (-8.7pp), and `trustly` (-8.3pp). The losses
are larger than the AND-gate per-stratum floor (`-0.15`, but `nuvei` is
already -0.10 — within tolerance).

## Reorder cases (positive — reranker rescued bi-encoder)

The reranker improved R@10 on 21/192 = 11% of queries. Five strongest cases:

1. `Return provider or business errors as declines during retries in GW`
   off=0.000 → on=1.000. Bi-encoder surfaced generic-knowledge files; reranker
   pulled the three actual paynearme decline-retry docs into ranks 6,7,10.

2. `payper interac etransfer task plan implementation changes files`
   off=0.000 → on=1.000. Bi-encoder collapsed to 7x duplicate
   `payper-docs/reference_interac-online.md`. Reranker diversified and
   surfaced the (unexpected!) Nuvei interac-etransfer doc the labeler
   actually wanted at rank 5.

3. `payper error 1006 authorization validation failed`
   off=0.333 → on=1.000. Reranker promoted
   `payper-docs/11-general-error-codes.md` from below top-10 into rank 1
   and surfaced both other expected error-code docs at ranks 5,6.

4. `payper error code 1006 authorization validation failed required fields`
   off=0.000 → on=0.500. Reranker pulled
   `payper-docs/reference_error-codes.md` from rank 11 into rank 4 (rank 9
   bi-encoder match was a sibling file but not in the expected set).

5. `providers-gateway initialize authorization validation check payment method type`
   off=0.000 → on=0.400. Reranker recovered
   `grpc-payment-gateway/docs/flows/grpc-payment-gateway.yaml` (rank 4) and
   `payment-flow.md` (rank 7) — both from outside the bi-encoder top-10.

## Reorder cases (negative — reranker pushed hits OUT)

The reranker hurt R@10 on 32/192 = 17% of queries. Two illustrative misses:

1. `paysafe handle-activities txType refund sale signalAsync child workflow re-fetch transaction`
   off=1.000 → on=0.000. Bi-encoder had the labeler-expected
   `paysafe-docs/.../multibanco-via-skrill-quick-checkout.md` at rank 10.
   Reranker dropped it (low surface-form match for "handle-activities
   refund sale workflow") in favour of `workflow-*` repo docs that
   share more vocabulary but are wrong files. **Likely a labeler-quality
   issue more than a reranker bug** — but the rerank-off bench
   accidentally rewarded the bi-encoder's tail-of-list keyword overlap.

2. `paysafe Interac webhook handler timeout 30 minutes 24 hours pending lookup`
   off=1.000 → on=0.000. Bi-encoder had the correct
   `paysafe-docs/.../interac-e-transfer.md` at ranks 1 AND 5 (two chunks
   from same file). Reranker downvoted the file entirely in favour of
   `workflow-paycom-notifications/.../performance-profile.md` (rank 1)
   and other webhook-named docs. Same pattern as #1: reranker rewards
   surface-form matches across the whole snippet, while the bi-encoder
   rewarded paysafe-docs file-name signal.

These are real reranker-bias signals, not labeler noise alone. They show that
rerank-on bench will catch regressions invisible to rerank-off bench.

## Latency

Reranker adds ~275 ms p50, ~536 ms p95 per query on Mac MPS (one CrossEncoder
forward pass over 50 candidates). At 192 queries the wall-clock for
encode + retrieve + rerank was 84.9s, vs 38.5s for the rerank-off run.

The AND-gate latency floor (`< 2x baseline p95`) is comfortably satisfied
(710/174 = 4.1x within-mode is irrelevant — gate compares candidate vs
baseline at the SAME mode, so future rerank-on candidates will be compared to
this 710 ms p95 baseline).

## Implications for re-bench / deploy decisions

- All future deploy decisions on the docs tower should run `--rerank-on` AND
  `--compare` against a rerank-on baseline. Comparing a candidate's rerank-on
  numbers to the historical rerank-off baseline is meaningless because the
  reranker can mask or invert retrieval-side gains.
- The rerank-off mode stays the default and stays useful for "is the new
  bi-encoder better?" questions in isolation — but it is no longer the
  authoritative deploy signal.
- The 32 rerank-on negative cases suggest one follow-up: a v2 of the
  reranker (e.g. fine-tuned on doc-intent pairs) might recover the
  paysafe/nuvei strata. Tracked but out of scope for this harness.

## How to use

    # Default (rerank-off, bi-encoder only — historical compatibility):
    python3.12 scripts/benchmark_doc_intent.py --model=docs --no-pre-flight \
        --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
        --out=/tmp/bench_v3_n200_docs.json

    # NEW E2E mode (rerank-on, full pipeline — deploy gate):
    python3.12 scripts/benchmark_doc_intent.py --model=docs --no-pre-flight \
        --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
        --rerank-on \
        --out=/tmp/bench_v3_n200_docs_e2e.json

    # AND-gate compare (works for both modes — manifest carries `rerank_on`):
    python3.12 scripts/benchmark_doc_intent.py \
        --compare /tmp/bench_v3_n200_docs_e2e.json /tmp/bench_v3_n200_candidate_e2e.json

## Constraints honoured

- DO NOT push to GitHub: respected. No `mcp__github__*` calls.
- DO NOT modify production model entries: respected. No edits to `src/models.py`,
  `src/embedding_provider.py`, `src/container.py`, or `src/search/hybrid.py`.
- pytest stays green: 829 passed, 1 skipped (was 822 + 1 skipped).
- Single-file edit to bench script: respected. One file changed in `scripts/`.
- Default `--rerank-off` path preserved: confirmed by
  `test_cli_rerank_off_default_skips_load`.
